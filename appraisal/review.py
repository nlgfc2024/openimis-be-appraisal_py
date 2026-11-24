"""An officer's desk screening (Operations Manual 8.2, Annex 2A): answering checklist items and recording the
eligibility decision. Called by the GraphQL mutations; rights are checked there, the rules here.

- An officer answers an item Yes, No or Pending. Yes and No need the evidence or source checked; Pending needs
  the clarification asked for. The answer is recorded against the officer and time, and clears basis_changed.
- Items can be answered only while the screening is open: before an eligibility decision, or after one that
  asked for clarification.
- A wrong decision is corrected by reopening the screening with a reason (the return right): the decision is
  cleared and the officer answers and decides again. Only ELIGIBLE and INELIGIBLE appraisals can be reopened,
  not those already scored or shortlisted.
- The decision follows the Annex 2A rule from the answers that count (the officer's, else the system's): any No
  is INELIGIBLE, otherwise any Pending is CLARIFICATION, otherwise ELIGIBLE. An item whose basis changed since
  the officer answered it must be answered again first, and the document check must be complete
  (appraisal.documents). An eligible appraisal is scored at once (appraisal.scoring).
- When the programme requires approval of decisions, the decision waits for it (appraisal.approvals) and the
  checklist can't be answered meanwhile.
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from appraisal.models import Appraisal, AppraisalItem

Result = AppraisalItem.Result
Eligibility, Stage, Status = Appraisal.Eligibility, Appraisal.Stage, Appraisal.Status

OPEN_STATUSES = (Status.RECEIVED, Status.DOCUMENT_CHECK, Status.DESK_SCREENING)
DECISION_LOG = "eligibility_decisions"
REOPEN_LOG = "reopened"
REOPENABLE = (Status.ELIGIBLE, Status.INELIGIBLE)


def _locked_appraisal(appraisal_id, user):
    """The appraisal, locked for this change, if the user may see it."""
    appraisal = Appraisal.get_queryset(Appraisal.objects.filter(id=appraisal_id, is_deleted=False), user) \
        .select_for_update(of=("self",)).first()
    if appraisal is None:
        raise ValidationError("appraisal.not_found")
    return appraisal


def _check_open(appraisal):
    from appraisal.approvals import Kind as ApprovalKind, check_not_pending

    if appraisal.status not in OPEN_STATUSES:
        raise ValidationError(f"appraisal.screening_closed: the appraisal is {appraisal.get_status_display().lower()}")
    check_not_pending(ApprovalKind.ELIGIBILITY, appraisal=appraisal)


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


def _decision(appraisal):
    """The checklist's decision (Annex 2A), once every item is answered, none changed and the documents checked;
    returns (decision, {key: result})."""
    checklist = appraisal.items.filter(kind=AppraisalItem.Kind.ELIGIBILITY, is_deleted=False)
    items = {item.key: item for item in checklist}
    keys = [rule["key"] for rule in (appraisal.config_snapshot or {}).get("eligibility", [])]
    missing = [key for key in keys if key not in items or items[key].result is None]
    if not keys or missing:
        raise ValidationError(f"appraisal.checklist_incomplete: {', '.join(missing) or 'no checklist'}")
    changed = [key for key in keys if items[key].basis_changed]
    if changed:
        raise ValidationError(f"appraisal.answers_changed: review {', '.join(changed)} again")
    from appraisal.documents import documents_problems

    problems = documents_problems(appraisal)
    if problems:
        listed = ", ".join(f"{label} ({status.lower()})" for label, status in problems)
        raise ValidationError(f"appraisal.documents_incomplete: {listed}")
    results = {key: items[key].result for key in keys}
    if Result.NO in results.values():
        return Eligibility.INELIGIBLE, results
    if Result.PENDING in results.values():
        return Eligibility.CLARIFICATION, results
    return Eligibility.ELIGIBLE, results


def _apply_decision(appraisal, user, decision, results, comment, requested_by=None, approved_by=None):
    appraisal.eligibility = decision
    appraisal.stage, appraisal.status = {
        Eligibility.ELIGIBLE: (Stage.DESK_SCORING, Status.ELIGIBLE),
        Eligibility.INELIGIBLE: (Stage.CLOSED, Status.INELIGIBLE),
        Eligibility.CLARIFICATION: (Stage.DESK_SCREENING, Status.DESK_SCREENING),
    }[decision]
    log = (appraisal.json_ext or {}).get(DECISION_LOG, [])
    entry = {"decision": decision, "by": (requested_by or user).username, "at": timezone.now().isoformat(),
             "comment": (comment or "").strip(),
             "ineligible": [key for key, result in results.items() if result == Result.NO],
             "pending": [key for key, result in results.items() if result == Result.PENDING]}
    if approved_by is not None:
        entry["approved_by"] = approved_by.username
    appraisal.json_ext = {**(appraisal.json_ext or {}), DECISION_LOG: [*log, entry]}
    appraisal.save(user=user)
    if decision == Eligibility.ELIGIBLE:
        from appraisal.scoring import score_appraisal

        score_appraisal(appraisal, user)


@transaction.atomic
def decide_eligibility(user, appraisal_id, comment=""):
    """Record the eligibility decision from the checklist, or ask for its approval when the programme requires it;
    returns the appraisal."""
    from appraisal.approvals import Kind as ApprovalKind, approval_required, request_approval

    appraisal = _locked_appraisal(appraisal_id, user)
    _check_open(appraisal)
    decision, results = _decision(appraisal)
    if approval_required(appraisal.config_snapshot, ApprovalKind.ELIGIBILITY):
        individual = appraisal.individual
        request_approval(user, ApprovalKind.ELIGIBILITY, appraisal.programme, appraisal.round,
                         {"decision": decision, "results": results, "comment": (comment or "").strip()},
                         {"applicant": f"{individual.first_name} {individual.last_name}".strip(),
                          "national_id": (individual.json_ext or {}).get("national_id"),
                          "decision": decision, "comment": (comment or "").strip()},
                         appraisal=appraisal)
        return appraisal
    _apply_decision(appraisal, user, decision, results, comment)
    return appraisal


def apply_decision_approval(approval, user):
    """Apply an approved eligibility decision, if the checklist still leads to it with the same answers."""
    appraisal = Appraisal.objects.filter(id=approval.appraisal_id, is_deleted=False) \
        .select_for_update(of=("self",)).first()
    if appraisal is None or appraisal.status not in OPEN_STATUSES:
        raise ValidationError("appraisal.screening_closed: the appraisal is no longer being screened")
    decision, results = _decision(appraisal)
    if decision != approval.proposal.get("decision") or results != approval.proposal.get("results"):
        raise ValidationError("appraisal.answers_changed: the checklist changed since the decision was asked for")
    _apply_decision(appraisal, user, decision, results, approval.proposal.get("comment"),
                    requested_by=approval.requested_by, approved_by=user)


@transaction.atomic
def reopen_screening(user, appraisal_id, reason):
    """Clear the eligibility decision so the screening can be done again; returns the appraisal."""
    appraisal = _locked_appraisal(appraisal_id, user)
    if appraisal.status not in REOPENABLE:
        raise ValidationError(f"appraisal.not_reopenable: the appraisal is {appraisal.get_status_display().lower()}")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("appraisal.reason_required: say why the decision is being corrected")
    log = (appraisal.json_ext or {}).get(REOPEN_LOG, [])
    entry = {"from": appraisal.status, "by": user.username, "at": timezone.now().isoformat(), "reason": reason}
    appraisal.json_ext = {**(appraisal.json_ext or {}), REOPEN_LOG: [*log, entry]}
    appraisal.eligibility = None
    appraisal.stage, appraisal.status = Stage.DESK_SCREENING, Status.DESK_SCREENING
    appraisal.save(user=user)
    return appraisal
