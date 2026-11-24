"""Desk scoring and the shortlist (Operations Manual Annex 2B, 4.1 stage 2).

- An eligible appraisal gets a SCORE item per configured component. Its system points come from the application's
  score input through enrolment's scoring rules (``component_points``), so appraisal and enrolment score alike.
  A missing or unreadable input scores the component's default; the basis says so for the officer.
- An officer may set a component's points (from 0 to its maximum) with the evidence checked.
- The total weighs each component's points by ``weight / maximum points`` and scales the sum to ``normalise_to``,
  as enrolment's weighted score does. With UMEP's weights equal to the maximum points it is the plain sum.
- Rescoring refreshes the system points under the appraisal's rules; an officer's points stay, and a component
  whose basis changed under them is flagged ``basis_changed``.
- The shortlist ranks the scored appraisals of a programme round by score (highest first), then submission time
  (earliest first), and marks the top ``per_city`` places of each city, or the top ``size`` overall, SHORTLISTED and
  the rest NOT_SHORTLISTED. The individual's ``json_ext.desk_appraisal_status`` records it for field validation.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from appraisal.models import Appraisal, AppraisalItem

Kind, Stage, Status = AppraisalItem.Kind, Appraisal.Stage, Appraisal.Status

SCORE_INPUTS = "score_inputs"
SHORTLIST_STATUS_KEY = "desk_appraisal_status"
SHORTLIST_LOG = "shortlist"
SCORABLE = (Status.ELIGIBLE, Status.SCORED)
RANKED = (Status.SCORED, Status.SHORTLISTED, Status.NOT_SHORTLISTED)
DEFAULT_CITY_LOCATION_TYPE = "R"
CENT = Decimal("0.01")


def _points(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def components(appraisal):
    """{key: component} of the appraisal's score components, in their configured order."""
    scoring = (appraisal.config_snapshot or {}).get("scoring") or {}
    return {component["key"]: component for component in scoring.get("components", [])}


def system_points(component, value):
    """(points, basis) the application's ``value`` gives ``component``."""
    from enrolment.ranking import component_points

    basis = {"field": component["field"], "value": value}
    try:
        points = component_points(component, value)
    except (TypeError, ValueError):
        points = component["scoring"].get("default", 0)
        basis["note"] = f"'{value}' can't be scored; an officer decides"
        return _points(points), basis
    if value is None:
        basis["note"] = "No answer; an officer decides"
    return _points(points), basis


def total(appraisal, items):
    """The appraisal's score from its items' points that count, None while any has no points."""
    from enrolment.ranking import maximum_points

    scoring = (appraisal.config_snapshot or {}).get("scoring") or {}
    weighted, weights = Decimal(0), Decimal(0)
    for key, component in components(appraisal).items():
        item = items.get(key)
        if item is None or item.points is None:
            return None
        weight = Decimal(str(component["weight"]))
        weighted += item.points * weight / Decimal(str(maximum_points(component["scoring"])))
        weights += weight
    if not weights:
        return None
    return _points(weighted * Decimal(str(scoring.get("normalise_to", 100))) / weights)


def _score_items(appraisal):
    return {item.key: item for item in appraisal.items.filter(kind=Kind.SCORE, is_deleted=False)}


def score_appraisal(appraisal, user):
    """Create or refresh the appraisal's score items and total; the appraisal must be eligible or scored."""
    if appraisal.status not in SCORABLE:
        raise ValidationError(f"appraisal.not_scorable: the appraisal is {appraisal.get_status_display().lower()}")
    if not components(appraisal):
        # A programme without score components stops at eligibility
        return appraisal
    inputs = (appraisal.json_ext or {}).get(SCORE_INPUTS, {})
    items = _score_items(appraisal)
    for key, component in components(appraisal).items():
        points, basis = system_points(component, inputs.get(component["field"]))
        item = items.get(key)
        if item is None:
            item = AppraisalItem(appraisal=appraisal, kind=Kind.SCORE, key=key, system_points=points, basis=basis)
            item.save(user=user)
            items[key] = item
        elif (item.system_points, item.basis) != (points, basis):
            if item.is_overridden:
                item.basis_changed = True
            item.system_points, item.basis = points, basis
            item.save(user=user)
    score = total(appraisal, items)
    if (appraisal.status, appraisal.stage, appraisal.score) != (Status.SCORED, Stage.DESK_SCORING, score):
        appraisal.status, appraisal.stage, appraisal.score, appraisal.rank = (
            Status.SCORED, Stage.DESK_SCORING, score, None)
        appraisal.save(user=user)
    return appraisal


def _locked(queryset):
    return queryset.select_for_update(of=("self",))


@transaction.atomic
def score_round(user, programme, round, ids=None):
    """Score every eligible or scored appraisal of the programme round the user can see (or only ``ids``)."""
    appraisals = Appraisal.get_queryset(
        Appraisal.objects.filter(programme=programme, round=round, status__in=SCORABLE, is_deleted=False), user)
    if ids:
        appraisals = appraisals.filter(id__in=ids)
    scored = [score_appraisal(appraisal, user) for appraisal in _locked(appraisals)]
    return len(scored)


@transaction.atomic
def answer_score(user, item_id, points, evidence="", comment=""):
    """Record the officer's points for one score component; returns the item."""
    item = AppraisalItem.objects.filter(id=item_id, kind=Kind.SCORE, is_deleted=False).first()
    if item is None:
        raise ValidationError("appraisal.item_not_found")
    appraisal = _locked(Appraisal.get_queryset(Appraisal.objects.filter(id=item.appraisal_id, is_deleted=False),
                                               user)).first()
    if appraisal is None:
        raise ValidationError("appraisal.not_found")
    if appraisal.status != Status.SCORED:
        raise ValidationError(f"appraisal.not_scorable: the appraisal is {appraisal.get_status_display().lower()}")
    from enrolment.ranking import maximum_points

    component = components(appraisal).get(item.key)
    if component is None:
        raise ValidationError("appraisal.item_not_found")
    highest = _points(maximum_points(component["scoring"]))
    try:
        points = _points(points)
    except (ArithmeticError, TypeError, ValueError):
        raise ValidationError("appraisal.invalid_points")
    if not Decimal(0) <= points <= highest:
        raise ValidationError(f"appraisal.invalid_points: from 0 to {highest}")
    evidence, comment = (evidence or "").strip(), (comment or "").strip()
    if not evidence:
        raise ValidationError("appraisal.evidence_required: record the evidence or source checked")
    item.officer_points, item.evidence, item.comment = points, evidence, comment
    item.decided_by, item.decided_at, item.basis_changed = user, timezone.now(), False
    item.save(user=user)
    appraisal.score = total(appraisal, _score_items(appraisal))
    appraisal.save(user=user)
    return item


def _city(location, location_type, cache):
    """The name of ``location``'s ancestor (or itself) of ``location_type``; None outside one. Cached by location."""
    if location is None:
        return None
    if location.id not in cache:
        place = location
        while place is not None and place.type != location_type:
            place = place.parent
        cache[location.id] = place.name if place is not None else None
    return cache[location.id]


def _rank_round(user, programme, round):
    """Score the round's eligible appraisals and rank the scored ones; returns (appraisals, ranking), the ranking
    [(group, places, members best first)] with the group the city, or None for an overall shortlist."""
    pending = Appraisal.objects.filter(programme=programme, round=round, status=Status.ELIGIBLE, is_deleted=False)
    for appraisal in _locked(pending):
        score_appraisal(appraisal, user)
    appraisals = list(_locked(Appraisal.objects.filter(programme=programme, round=round, status__in=RANKED,
                                                       is_deleted=False))
                      .select_related("individual", "submission"))
    if not appraisals:
        raise ValidationError("appraisal.nothing_to_shortlist")
    changed = AppraisalItem.objects.filter(appraisal__in=appraisals, kind=Kind.SCORE, basis_changed=True,
                                           is_deleted=False).values_list("appraisal_id", flat=True).distinct()
    if changed.exists():
        raise ValidationError(f"appraisal.scores_changed: {changed.count()} appraisals to review first")
    unscored = [appraisal for appraisal in appraisals if appraisal.score is None]
    if unscored:
        raise ValidationError(f"appraisal.scores_missing: {len(unscored)} appraisals without a score")

    rules = (appraisals[0].config_snapshot or {}).get("shortlist") or {}
    per_city, size = rules.get("per_city"), rules.get("size")
    if not per_city and not size:
        raise ValidationError("appraisal.no_shortlist_rule: set shortlist.size or shortlist.per_city")
    location_type, cache, groups = rules.get("location_type", DEFAULT_CITY_LOCATION_TYPE), {}, {}
    for appraisal in appraisals:
        group = _city(appraisal.individual.location, location_type, cache) if per_city else None
        groups.setdefault(group, []).append(appraisal)

    def order(appraisal):
        submitted = (appraisal.submission.submitted_at if appraisal.submission_id else None) or appraisal.date_created
        kobo = appraisal.submission.kobo_id if appraisal.submission_id else 0
        return -appraisal.score, submitted.timestamp() if submitted else 0, kobo, str(appraisal.id)

    ranking = []
    for group, members in groups.items():
        members.sort(key=order)
        ranking.append((group, per_city.get(group, 0) if per_city else size, members))
    return appraisals, ranking


def _proposal(ranking):
    """The shortlist as data, to keep for approval and compare when it is applied."""
    return [{"id": str(appraisal.id), "group": group, "rank": position, "shortlisted": position <= places,
             "score": str(appraisal.score)}
            for group, places, members in ranking for position, appraisal in enumerate(members, start=1)]


def _summary(ranking):
    return {group: {"places": places, "ranked": len(members), "shortlisted": min(places, len(members))}
            for group, places, members in ranking}


def _apply_shortlist(user, appraisals, ranking, approved_by=None, requested_by=None):
    for group, places, members in ranking:
        for position, appraisal in enumerate(members, start=1):
            _set_shortlisted(appraisal, position, position <= places, user)
    log = {"by": (requested_by or user).username, "at": timezone.now().isoformat(),
           "groups": [{"group": group, **counts} for group, counts in _summary(ranking).items()]}
    if approved_by is not None:
        log["approved_by"] = approved_by.username
    for appraisal in appraisals:
        appraisal.json_ext = {**(appraisal.json_ext or {}), SHORTLIST_LOG: log}
        appraisal.save(user=user)


@transaction.atomic
def shortlist_round(user, programme, round):
    """Rank the round's scored appraisals and shortlist the top places, by city when configured; when the programme
    requires approval, the shortlist waits for it (appraisal.approvals) instead.

    Returns {group: {"places", "ranked", "shortlisted"}}; the group is the city, or None for an overall shortlist.
    """
    from appraisal.approvals import Kind as ApprovalKind, approval_required, check_not_pending, request_approval

    check_not_pending(ApprovalKind.SHORTLIST, programme, round)
    appraisals, ranking = _rank_round(user, programme, round)
    summary = _summary(ranking)
    if approval_required(appraisals[0].config_snapshot, ApprovalKind.SHORTLIST):
        groups = [{"group": group, **counts} for group, counts in summary.items()]
        request_approval(user, ApprovalKind.SHORTLIST, programme, round, {"ranking": _proposal(ranking)}, {
            "groups": groups,
            "ranked": sum(group["ranked"] for group in groups),
            "shortlisted": sum(group["shortlisted"] for group in groups),
        })
        return summary
    _apply_shortlist(user, appraisals, ranking)
    return summary


def apply_shortlist_approval(approval, user):
    """Apply an approved shortlist, if the round still ranks as it did when it was asked for."""
    appraisals, ranking = _rank_round(user, approval.programme, approval.round)
    if _proposal(ranking) != approval.proposal.get("ranking"):
        raise ValidationError("appraisal.shortlist_changed: scores or applicants changed since the shortlist was "
                              "asked for; run it again")
    _apply_shortlist(user, appraisals, ranking, approved_by=user, requested_by=approval.requested_by)


def _set_shortlisted(appraisal, rank, shortlisted, user):
    status = Status.SHORTLISTED if shortlisted else Status.NOT_SHORTLISTED
    appraisal.status, appraisal.stage, appraisal.rank = status, Stage.SHORTLISTING, rank
    individual = appraisal.individual
    if (individual.json_ext or {}).get(SHORTLIST_STATUS_KEY) != status:
        individual.json_ext = {**(individual.json_ext or {}), SHORTLIST_STATUS_KEY: status}
        individual.save(user=user)
