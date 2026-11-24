"""Return for action (Operations Manual 8.8d).

- An appraisal still being screened or scored can be returned, with the reason and the action required. It becomes
  RETURNED, keeping its stage, and nothing can be answered, decided or scored until it is resubmitted.
- Resubmission closes the return (who, when, a note) and restores the status it had. A new application from the
  applicant resubmits it automatically (services.apply_submission).
- Every return stays on the appraisal with its dates, so the history shows each return and resubmission.
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from appraisal.models import Appraisal, AppraisalReturn

Status = Appraisal.Status
RETURNABLE = (Status.RECEIVED, Status.DOCUMENT_CHECK, Status.DESK_SCREENING, Status.ELIGIBLE, Status.SCORED)


def _locked(user, appraisal_id):
    appraisal = Appraisal.get_queryset(Appraisal.objects.filter(id=appraisal_id, is_deleted=False), user) \
        .select_for_update(of=("self",)).first()
    if appraisal is None:
        raise ValidationError("appraisal.not_found")
    return appraisal


def return_problem(appraisal):
    """Why the appraisal can't be returned for action now, or None (the rule return_appraisal applies)."""
    if appraisal.status not in RETURNABLE:
        return f"appraisal.not_returnable: the appraisal is {appraisal.get_status_display().lower()}"
    from appraisal.approvals import Kind as ApprovalKind, check_not_pending

    # A decision awaiting approval is approved or rejected first, so no task is left open on a returned appraisal
    try:
        check_not_pending(ApprovalKind.ELIGIBILITY, appraisal=appraisal)
    except ValidationError as exc:
        return exc.messages[0]
    return None


def can_resubmit(appraisal):
    """Whether the appraisal has an open return to close (the rule resubmit_appraisal applies)."""
    return appraisal.status == Status.RETURNED and \
        appraisal.returns.filter(is_deleted=False, resubmitted_at__isnull=True).exists()


@transaction.atomic
def return_appraisal(user, appraisal_id, reason, action_required):
    """Return the appraisal for action; returns the AppraisalReturn."""
    appraisal = _locked(user, appraisal_id)
    problem = return_problem(appraisal)
    if problem:
        raise ValidationError(problem)
    reason, action_required = (reason or "").strip(), (action_required or "").strip()
    if not reason or not action_required:
        raise ValidationError("appraisal.return_details_required: give the reason and the action required")
    returned = AppraisalReturn(appraisal=appraisal, stage=appraisal.stage, previous_status=appraisal.status,
                               reason=reason, action_required=action_required, requested_by=user,
                               returned_at=timezone.now())
    returned.save(user=user)
    appraisal.status = Status.RETURNED
    appraisal.save(user=user)
    return returned


def _resubmit(appraisal, user, note):
    if not can_resubmit(appraisal):
        raise ValidationError("appraisal.not_returned")
    open_return = appraisal.returns.filter(is_deleted=False, resubmitted_at__isnull=True).first()
    open_return.resubmitted_by, open_return.resubmitted_at = user, timezone.now()
    open_return.resubmission_note = (note or "").strip()
    open_return.save(user=user)
    appraisal.status = open_return.previous_status
    appraisal.save(user=user)
    return open_return


@transaction.atomic
def resubmit_appraisal(user, appraisal_id, note=""):
    """Close the open return and restore the appraisal's status; returns the AppraisalReturn."""
    return _resubmit(_locked(user, appraisal_id), user, note)


def resubmit_on_new_application(appraisal, submission, user):
    """A returned appraisal receiving a newer application is resubmitted (called inside the import's transaction)."""
    if appraisal.status == Status.RETURNED:
        _resubmit(appraisal, user, f"New application from Kobo (submission {submission.kobo_id})")
