"""Desk scoring and the shortlist (Operations Manual Annex 2B, 4.1 stage 2).

- An eligible appraisal gets a SCORE item per configured component. Its system points come from the application's
  score input through enrolment's scoring rules (``component_points``), so appraisal and enrolment score alike.
  A missing or unreadable input leaves the component without points until an officer sets them, so the total stays
  empty and the shortlist can't run with it.
- An officer corrects a component's value (the answer, as the evidence shows it) with the evidence checked; the
  points follow from the same scoring rules, so they never depart from the rubric.
- The total weighs each component's points by ``weight / maximum points`` and scales the sum to ``normalise_to``,
  as enrolment's weighted score does. With UMEP's weights equal to the maximum points it is the plain sum.
- Rescoring refreshes the system points under the appraisal's rules; an officer's points stay, and a component
  whose basis changed under them is flagged ``basis_changed``.
- The shortlist ranks the scored appraisals of a programme round by score (highest first), then submission time
  (earliest first), and fills the round's ``places`` with enrolment's selector: the ``selection_strategy`` quotas
  (e.g. per business city, with ``location_code`` conditions) in order, then, with ``spillover``, the next ranked.
  Its rules are the round's, read from the current configuration. Each appraisal is ranked within the first quota it
  matches (``json_ext.shortlist_group``). With ``shortlist.individual_field`` the outcome is also written to the
  individual, through the individual service, for field validation and enrolment to select from.
- Closing the shortlist makes the round's outcome final: its appraisals move to stage CLOSED and the shortlist
  can't be run for the round again.
"""
import math
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from appraisal.models import Appraisal, AppraisalItem

Kind, Stage, Status = AppraisalItem.Kind, Appraisal.Stage, Appraisal.Status

SCORE_INPUTS = "score_inputs"
SHORTLIST_LOG = "shortlist"
SHORTLIST_GROUP = "shortlist_group"
# An officer's corrected value of a score item, in its json_ext; its points are the officer points
OFFICER_VALUE = "officer_value"
SCORABLE = (Status.ELIGIBLE, Status.SCORED)
RANKED = (Status.SCORED, Status.SHORTLISTED, Status.NOT_SHORTLISTED)
OUTCOMES = (Status.SHORTLISTED, Status.NOT_SHORTLISTED)
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
    # No points rather than the default, so nobody is ranked on a value no one has looked at
    if value is None:
        basis["note"] = "No answer; an officer decides"
        return None, basis
    try:
        points = component_points(component, value)
    except (TypeError, ValueError):
        basis["note"] = f"'{value}' can't be scored; an officer decides"
        return None, basis
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
def answer_score(user, item_id, value, evidence="", comment=""):
    """Record the officer's corrected value for one score component; its points follow from the component's
    scoring rules. Returns the item."""
    item = AppraisalItem.objects.filter(id=item_id, kind=Kind.SCORE, is_deleted=False).first()
    if item is None:
        raise ValidationError("appraisal.item_not_found")
    appraisal = _locked(Appraisal.get_queryset(Appraisal.objects.filter(id=item.appraisal_id, is_deleted=False),
                                               user)).first()
    if appraisal is None:
        raise ValidationError("appraisal.not_found")
    if appraisal.status != Status.SCORED:
        raise ValidationError(f"appraisal.not_scorable: the appraisal is {appraisal.get_status_display().lower()}")
    component = components(appraisal).get(item.key)
    if component is None:
        raise ValidationError("appraisal.item_not_found")
    value = str(value if value is not None else "").strip()
    points, basis = system_points(component, value or None)
    if points is None:
        raise ValidationError(f"appraisal.invalid_value: {basis.get('note') or value}")
    evidence, comment = (evidence or "").strip(), (comment or "").strip()
    if not evidence:
        raise ValidationError("appraisal.evidence_required: record the evidence or source checked")
    item.officer_points, item.evidence, item.comment = points, evidence, comment
    item.json_ext = {**(item.json_ext or {}), OFFICER_VALUE: value}
    item.decided_by, item.decided_at, item.basis_changed = user, timezone.now(), False
    item.save(user=user)
    appraisal.score = total(appraisal, _score_items(appraisal))
    appraisal.save(user=user)
    return item


def round_rules(programme, round):
    """The shortlist rules of ``programme``'s ``round`` from the current configuration: they belong to the round,
    not to each appraisal. Refused when the programme no longer configures that round or has no shortlist."""
    from appraisal.apps import AppraisalConfig

    AppraisalConfig.check_configured()
    config = AppraisalConfig.programme(programme)
    if not config or config.get("round") != round:
        raise ValidationError(f"appraisal.round_not_configured: {programme} is not configured for round {round}")
    rules = config.get("shortlist")
    if not rules:
        raise ValidationError("appraisal.no_shortlist_rule: set the programme's shortlist places")
    return rules


def _order(appraisal):
    """Best first: score, then submission time and Kobo id, so ties never depend on row order."""
    submitted = (appraisal.submission.submitted_at if appraisal.submission_id else None) or appraisal.date_created
    kobo = appraisal.submission.kobo_id if appraisal.submission_id else 0
    return -appraisal.score, submitted.timestamp() if submitted else 0, kobo, str(appraisal.id)


def _membership(appraisals, rules):
    """(quotas, spillover, {quota key: individual ids}, {appraisal id: the first quota it matches or None}) under the
    shortlist ``rules``; no quotas when they set no selection_strategy."""
    from enrolment.ranking import normalise_selection_strategy, quota_members
    from individual.models import Individual

    strategy = (rules or {}).get("selection_strategy")
    if not strategy:
        return [], True, {}, {appraisal.id: None for appraisal in appraisals}
    strategy = normalise_selection_strategy(strategy, Individual)
    quotas = strategy["quotas"]
    members = quota_members(Individual.objects.filter(id__in=[a.individual_id for a in appraisals]), quotas)

    def first_quota(appraisal):
        return next((quota["key"] for quota in quotas if appraisal.individual_id in members[quota["key"]]), None)

    return quotas, strategy["spillover"], members, {appraisal.id: first_quota(appraisal) for appraisal in appraisals}


def quota_groups(appraisals, rules):
    """({appraisal id: its quota or None}, {quota: its places}) under the shortlist ``rules``, for the report."""
    quotas, _spillover, _members, group_of = _membership(appraisals, rules)
    if not rules:
        return group_of, {}
    if not quotas:
        return group_of, {None: rules["places"]}
    return group_of, {quota["key"]: quota.get("count", math.floor(rules["places"] * quota.get("percentage", 0) / 100))
                      for quota in quotas}


def _allocate(appraisals, rules):
    """Fill the round's places with enrolment's selector. Returns (selected individual ids, groups, group of each
    appraisal), the groups [{"group", "places", "ranked", "shortlisted"}] per quota then the places left over
    (group None)."""
    from enrolment.ranking import allocate_places

    places = rules["places"]
    quotas, spillover, members, group_of = _membership(appraisals, rules)
    ranked = [appraisal.individual_id for appraisal in appraisals]
    selected, fill = allocate_places(ranked, quotas, members, places, spillover)
    groups = [{"group": bucket["key"], "places": bucket["reserved"],
               "ranked": sum(1 for group in group_of.values() if group == bucket["key"]),
               "shortlisted": bucket["filled"]} for bucket in fill["buckets"]]
    outside = sum(1 for group in group_of.values() if group is None)
    left = places - sum(bucket["reserved"] for bucket in fill["buckets"])
    if outside or (spillover and left):
        groups.append({"group": None, "places": left if spillover else 0, "ranked": outside,
                       "shortlisted": fill["unreserved"]})
    return set(selected), groups, group_of


def _check_round_open(programme, round):
    closed = Appraisal.objects.filter(programme=programme, round=round, stage=Stage.CLOSED, status__in=OUTCOMES,
                                      is_deleted=False)
    if closed.exists():
        raise ValidationError(f"appraisal.shortlist_closed: the {programme} {round} shortlist is closed")


def _rank_round(user, programme, round):
    """Score the round's eligible appraisals and rank the scored ones under the round's rules. Returns (appraisals
    best first, rules, outcomes [(appraisal, group, rank, shortlisted)], groups)."""
    rules = round_rules(programme, round)
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

    appraisals.sort(key=_order)
    selected, groups, group_of = _allocate(appraisals, rules)
    positions, outcomes = {}, []
    for appraisal in appraisals:
        group = group_of[appraisal.id]
        positions[group] = positions.get(group, 0) + 1
        outcomes.append((appraisal, group, positions[group], appraisal.individual_id in selected))
    return appraisals, rules, outcomes, groups


def _proposal(outcomes):
    """The shortlist as data, to keep for approval and compare when it is applied."""
    return [{"id": str(appraisal.id), "group": group, "rank": rank, "shortlisted": shortlisted,
             "score": str(appraisal.score)} for appraisal, group, rank, shortlisted in outcomes]


def _summary(groups):
    return {group["group"]: {key: group[key] for key in ("places", "ranked", "shortlisted")} for group in groups}


def _apply_shortlist(user, appraisals, rules, outcomes, groups, approved_by=None, requested_by=None):
    for appraisal, group, rank, shortlisted in outcomes:
        _set_shortlisted(appraisal, rank, shortlisted, group, rules.get("individual_field"), user)
    log = {"by": (requested_by or user).username, "at": timezone.now().isoformat(), "groups": groups}
    if approved_by is not None:
        log["approved_by"] = approved_by.username
    for appraisal in appraisals:
        appraisal.json_ext = {**(appraisal.json_ext or {}), SHORTLIST_LOG: log}
        appraisal.save(user=user)


@transaction.atomic
def shortlist_round(user, programme, round):
    """Rank the round's scored appraisals and shortlist its places, by quota (e.g. per city) when configured; when
    the programme requires approval, the shortlist waits for it (appraisal.approvals) instead.

    Returns {group: {"places", "ranked", "shortlisted"}}; the group is the quota key, or None for the other places.
    """
    from appraisal.apps import AppraisalConfig
    from appraisal.approvals import Kind as ApprovalKind, approval_required, check_not_pending, request_approval

    _check_round_open(programme, round)
    check_not_pending(ApprovalKind.SHORTLIST, programme, round)
    appraisals, rules, outcomes, groups = _rank_round(user, programme, round)
    if approval_required(AppraisalConfig.programme(programme), ApprovalKind.SHORTLIST):
        request_approval(user, ApprovalKind.SHORTLIST, programme, round, {"ranking": _proposal(outcomes)}, {
            "groups": groups,
            "ranked": sum(group["ranked"] for group in groups),
            "shortlisted": sum(group["shortlisted"] for group in groups),
        })
        return _summary(groups)
    _apply_shortlist(user, appraisals, rules, outcomes, groups)
    return _summary(groups)


def apply_shortlist_approval(approval, user):
    """Apply an approved shortlist, if the round still ranks as it did when it was asked for."""
    _check_round_open(approval.programme, approval.round)
    appraisals, rules, outcomes, groups = _rank_round(user, approval.programme, approval.round)
    if _proposal(outcomes) != approval.proposal.get("ranking"):
        raise ValidationError("appraisal.shortlist_changed: scores or applicants changed since the shortlist was "
                              "asked for; run it again")
    _apply_shortlist(user, appraisals, rules, outcomes, groups, approved_by=user, requested_by=approval.requested_by)


def _set_shortlisted(appraisal, rank, shortlisted, group, individual_field, user):
    status = Status.SHORTLISTED if shortlisted else Status.NOT_SHORTLISTED
    appraisal.status, appraisal.stage, appraisal.rank = status, Stage.SHORTLISTING, rank
    # The quota (e.g. the city) the appraisal was ranked in, for the ranking screen; None for the other places
    appraisal.json_ext = {**(appraisal.json_ext or {}), SHORTLIST_GROUP: group}
    if individual_field:
        _record_on_individual(appraisal.individual, individual_field, status, user)


def _record_on_individual(individual, field, status, user):
    """Write the outcome to the individual's ``field`` through the individual service, for field validation."""
    from individual.services import IndividualService

    if (individual.json_ext or {}).get(field) == status:
        return
    result = IndividualService(user).update({"id": individual.id,
                                             "json_ext": {**(individual.json_ext or {}), field: status}})
    if not result or not result.get("success"):
        raise ValidationError(f"appraisal.individual_not_updated: {(result or {}).get('detail') or individual.id}")


@transaction.atomic
def close_shortlist(user, programme, round):
    """Make the round's shortlist final; returns how many appraisals it closed."""
    from appraisal.approvals import Kind as ApprovalKind, check_not_pending

    _check_round_open(programme, round)
    check_not_pending(ApprovalKind.SHORTLIST, programme, round)
    appraisals = list(_locked(Appraisal.objects.filter(programme=programme, round=round, status__in=RANKED,
                                                       is_deleted=False)))
    unranked = [appraisal for appraisal in appraisals if appraisal.status == Status.SCORED]
    if unranked:
        raise ValidationError(f"appraisal.shortlist_outdated: {len(unranked)} scored appraisals aren't ranked; "
                              "run the shortlist first")
    if not appraisals:
        raise ValidationError("appraisal.nothing_to_shortlist")
    closed = {"by": user.username, "at": timezone.now().isoformat()}
    for appraisal in appraisals:
        log = (appraisal.json_ext or {}).get(SHORTLIST_LOG) or {}
        appraisal.json_ext = {**(appraisal.json_ext or {}), SHORTLIST_LOG: {**log, "closed": closed}}
        appraisal.stage = Stage.CLOSED
        appraisal.save(user=user)
    return len(appraisals)
