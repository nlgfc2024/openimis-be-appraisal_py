"""An officer's desk screening (Operations Manual 8.2, Annex 2A): answering checklist items and recording the
eligibility decision. Called by the GraphQL mutations; rights are checked there, the rules here.

- An officer answers an item Yes, No or Pending. Yes and No need the evidence or source checked; Pending needs
  the clarification asked for. The answer is recorded against the officer and time, and clears basis_changed.
- Items can be answered only while the screening is open: before an eligibility decision, or after one that
  asked for clarification.
- The decision follows the Annex 2A rule from the answers that count (the officer's, else the system's): any No
  is INELIGIBLE, otherwise any Pending is CLARIFICATION, otherwise ELIGIBLE. An item whose basis changed since
  the officer answered it must be answered again first. An eligible appraisal is scored at once (appraisal.scoring).
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from appraisal.models import Appraisal, AppraisalItem

Result = AppraisalItem.Result
Eligibility, Stage, Status = Appraisal.Eligibility, Appraisal.Stage, Appraisal.Status

OPEN_STATUSES = (Status.RECEIVED, Status.DOCUMENT_CHECK, Status.DESK_SCREENING)
DECISION_LOG = "eligibility_decisions"


def _locked_appraisal(appraisal_id, user):
    """The appraisal, locked for this change, if the user may see it."""
    appraisal = Appraisal.get_queryset(Appraisal.objects.filter(id=appraisal_id, is_deleted=False), user) \
        .select_for_update(of=("self",)).first()
    if appraisal is None:
        raise ValidationError("appraisal.not_found")
    return appraisal


def _check_open(appraisal):
    if appraisal.status not in OPEN_STATUSES:
        raise ValidationError(f"appraisal.screening_closed: the appraisal is {appraisal.get_status_display().lower()}")


@transaction.atomic
def answer_item(user, item_id, result, evidence="", comment=""):
    """Record the officer's answer to an eligibility item; returns the item."""
    item = AppraisalItem.objects.filter(id=item_id, is_deleted=False).first()
    if item is None:
        raise ValidationError("appraisal.item_not_found")
    appraisal = _locked_appraisal(item.appraisal_id, user)
    _check_open(appraisal)
    if item.kind != AppraisalItem.Kind.ELIGIBILITY:
        raise ValidationError("appraisal.item_not_eligibility: score items are answered when scoring")
    if result not in Result.values:
        raise ValidationError(f"appraisal.invalid_result: {result}")
    evidence, comment = (evidence or "").strip(), (comment or "").strip()
    if result in (Result.YES, Result.NO) and not evidence:
        raise ValidationError("appraisal.evidence_required: record the evidence or source checked")
    if result == Result.PENDING and not comment:
        raise ValidationError("appraisal.clarification_required: say what needs clarifying")
    item.officer_result, item.evidence, item.comment = result, evidence, comment
    item.decided_by, item.decided_at, item.basis_changed = user, timezone.now(), False
    item.save(user=user)
    if appraisal.status != Status.DESK_SCREENING:
        appraisal.stage, appraisal.status = Stage.DESK_SCREENING, Status.DESK_SCREENING
        appraisal.save(user=user)
    return item


@transaction.atomic
def decide_eligibility(user, appraisal_id, comment=""):
    """Record the eligibility decision from the checklist; returns the appraisal."""
    appraisal = _locked_appraisal(appraisal_id, user)
    _check_open(appraisal)
    items = {item.key: item for item in appraisal.items.filter(kind=AppraisalItem.Kind.ELIGIBILITY,
                                                                 is_deleted=False)}
    keys = [rule["key"] for rule in (appraisal.config_snapshot or {}).get("eligibility", [])]
    missing = [key for key in keys if key not in items or items[key].result is None]
    if not keys or missing:
        raise ValidationError(f"appraisal.checklist_incomplete: {', '.join(missing) or 'no checklist'}")
    changed = [key for key in keys if items[key].basis_changed]
    if changed:
        raise ValidationError(f"appraisal.answers_changed: review {', '.join(changed)} again")
    results = [items[key].result for key in keys]
    if Result.NO in results:
        decision = Eligibility.INELIGIBLE
    elif Result.PENDING in results:
        decision = Eligibility.CLARIFICATION
    else:
        decision = Eligibility.ELIGIBLE
    appraisal.eligibility = decision
    appraisal.stage, appraisal.status = {
        Eligibility.ELIGIBLE: (Stage.DESK_SCORING, Status.ELIGIBLE),
        Eligibility.INELIGIBLE: (Stage.CLOSED, Status.INELIGIBLE),
        Eligibility.CLARIFICATION: (Stage.DESK_SCREENING, Status.DESK_SCREENING),
    }[decision]
    log = (appraisal.json_ext or {}).get(DECISION_LOG, [])
    entry = {"decision": decision, "by": user.username, "at": timezone.now().isoformat(),
             "comment": (comment or "").strip(),
             "ineligible": [key for key in keys if items[key].result == Result.NO],
             "pending": [key for key in keys if items[key].result == Result.PENDING]}
    appraisal.json_ext = {**(appraisal.json_ext or {}), DECISION_LOG: [*log, entry]}
    appraisal.save(user=user)
    if decision == Eligibility.ELIGIBLE:
        from appraisal.scoring import score_appraisal

        score_appraisal(appraisal, user)
    return appraisal
