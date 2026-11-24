"""Desk scoring and the shortlist (Operations Manual Annex 2B, 4.1 stage 2).

- An eligible appraisal gets a SCORE item per configured component. Its system points come from the application's
  score input through enrolment's scoring rules (``component_points``), so appraisal and enrolment score alike.
  A missing or unreadable input leaves the component without points until an officer sets them, so the total stays
  empty and the shortlist can't run with it.
- An officer may set a component's points (from 0 to its maximum) with the evidence checked.
- The total weighs each component's points by ``weight / maximum points`` and scales the sum to ``normalise_to``,
  as enrolment's weighted score does. With UMEP's weights equal to the maximum points it is the plain sum.
- Rescoring refreshes the system points under the appraisal's rules; an officer's points stay, and a component
  whose basis changed under them is flagged ``basis_changed``.
- The shortlist ranks the scored appraisals of a programme round by score (highest first), then submission time
  (earliest first), and marks the top ``per_city`` places of each city, or the top ``size`` overall, SHORTLISTED and
  the rest NOT_SHORTLISTED. The city is the business location's answer (``shortlist.city``) or else the individual's
  location; an applicant whose city has no places stops the shortlist. All appraisals of the round must hold the
  same shortlist rules. With ``shortlist.individual_field`` the outcome is also written to the individual, through
  the individual service, for field validation to select from.
- Closing the shortlist makes the round's outcome final: its appraisals move to stage CLOSED and the shortlist
  can't be run for the round again.
"""
import json
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from appraisal.models import Appraisal, AppraisalItem

Kind, Stage, Status = AppraisalItem.Kind, Appraisal.Stage, Appraisal.Status

SCORE_INPUTS = "score_inputs"
SHORTLIST_LOG = "shortlist"
SCORABLE = (Status.ELIGIBLE, Status.SCORED)
RANKED = (Status.SCORED, Status.SHORTLISTED, Status.NOT_SHORTLISTED)
OUTCOMES = (Status.SHORTLISTED, Status.NOT_SHORTLISTED)
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


def _round_rules(appraisals):
    """The shortlist rules the round's appraisals hold; refused when their snapshots disagree."""
    rules = {json.dumps((a.config_snapshot or {}).get("shortlist") or {}, sort_keys=True) for a in appraisals}
    if len(rules) > 1:
        raise ValidationError("appraisal.shortlist_rules_differ: the round's appraisals were created under "
                              "different shortlist rules; settle them before shortlisting")
    return json.loads(rules.pop())


def _ancestor_name(location, location_type):
    while location is not None and location.type != location_type:
        location = location.parent
    return location.name if location is not None else None


def _city_finder(rules):
    """appraisal -> the name of its city: the location answered to ``city.field`` (a ``city.location_type`` code,
    followed to its current code) or else the individual's location, up to its ``location_type`` ancestor."""
    from location.models import Location

    group_type = rules.get("location_type", DEFAULT_CITY_LOCATION_TYPE)
    city = rules.get("city")
    cache = {}

    def by_answer(appraisal):
        from msr_etl.location_file import current_location_code

        code = (appraisal.individual.json_ext or {}).get(city["field"])
        if code in (None, ""):
            return None
        key = str(code).strip()
        if key not in cache:
            current = current_location_code(city["location_type"], key)
            location = Location.objects.filter(code=current, type=city["location_type"], validity_to__isnull=True) \
                .first()
            cache[key] = _ancestor_name(location, group_type)
        return cache[key]

    def by_residence(appraisal):
        location = appraisal.individual.location
        if location is None:
            return None
        if location.id not in cache:
            cache[location.id] = _ancestor_name(location, group_type)
        return cache[location.id]

    return by_answer if city else by_residence


def _check_round_open(programme, round):
    closed = Appraisal.objects.filter(programme=programme, round=round, stage=Stage.CLOSED, status__in=OUTCOMES,
                                      is_deleted=False)
    if closed.exists():
        raise ValidationError(f"appraisal.shortlist_closed: the {programme} {round} shortlist is closed")


@transaction.atomic
def shortlist_round(user, programme, round):
    """Rank the round's scored appraisals and shortlist the top places, by city when configured.

    Returns {group: {"places", "ranked", "shortlisted"}}; the group is the city, or None for an overall shortlist.
    """
    _check_round_open(programme, round)
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

    rules = _round_rules(appraisals)
    per_city, size = rules.get("per_city"), rules.get("size")
    if not per_city and not size:
        raise ValidationError("appraisal.no_shortlist_rule: set shortlist.size or shortlist.per_city")
    groups = {}
    if per_city:
        city_of = _city_finder(rules)
        for appraisal in appraisals:
            groups.setdefault(city_of(appraisal), []).append(appraisal)
        unplaced = {city: len(members) for city, members in groups.items() if city not in per_city}
        if unplaced:
            listed = ", ".join(f"{city or 'no city'} ({count})" for city, count in sorted(
                unplaced.items(), key=lambda entry: str(entry[0])))
            raise ValidationError(f"appraisal.city_without_places: {listed}; places are set for "
                                  f"{', '.join(per_city)}")
    else:
        groups[None] = appraisals

    def order(appraisal):
        submitted = (appraisal.submission.submitted_at if appraisal.submission_id else None) or appraisal.date_created
        kobo = appraisal.submission.kobo_id if appraisal.submission_id else 0
        return -appraisal.score, submitted.timestamp() if submitted else 0, kobo, str(appraisal.id)

    summary = {}
    for group, members in groups.items():
        places = per_city[group] if per_city else size
        members.sort(key=order)
        for position, appraisal in enumerate(members, start=1):
            _set_shortlisted(appraisal, position, position <= places, rules.get("individual_field"), user)
        summary[group] = {"places": places, "ranked": len(members), "shortlisted": min(places, len(members))}
    log = {"by": user.username, "at": timezone.now().isoformat(),
           "groups": [{"group": group, **counts} for group, counts in summary.items()]}
    for appraisal in appraisals:
        appraisal.json_ext = {**(appraisal.json_ext or {}), SHORTLIST_LOG: log}
        appraisal.save(user=user)
    return summary


def _set_shortlisted(appraisal, rank, shortlisted, individual_field, user):
    status = Status.SHORTLISTED if shortlisted else Status.NOT_SHORTLISTED
    appraisal.status, appraisal.stage, appraisal.rank = status, Stage.SHORTLISTING, rank
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
    _check_round_open(programme, round)
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
