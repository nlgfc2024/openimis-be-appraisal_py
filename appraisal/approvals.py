"""Supervisor approval (Operations Manual 8.12) through tasks_management's maker-checker.

- A programme sets ``approval.shortlist`` and/or ``approval.eligibility``. The officer's change is then kept as an
  AppraisalApproval with a task in tasks_management, and nothing changes on the appraisals until it is approved.
- The task is approved or rejected in the Tasks screen by its task group (source ``appraisal``), under the group's
  completion policy. The approver needs ``gql_mutation_approve_appraisal_perms``; otherwise nothing is applied. On
  approval the change is checked again against the appraisals as they are now and applied;
  if they changed meanwhile it is not applied (FAILED, with the reason) and the officer runs it again.
- One shortlist per round and one decision per appraisal can wait for approval at a time.
"""
import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from appraisal.models import AppraisalApproval

logger = logging.getLogger(__name__)

Kind, ApprovalStatus = AppraisalApproval.Kind, AppraisalApproval.Status
TASK_SOURCE = "appraisal"
BUSINESS_EVENTS = {Kind.SHORTLIST: "appraisal_shortlist", Kind.ELIGIBILITY: "appraisal_eligibility"}


def approval_required(programme, kind):
    """Whether the programme's current configuration requires approval for ``kind`` (a round rule)."""
    return bool(((programme or {}).get("approval") or {}).get(kind.lower()))


def pending_approval(kind, programme=None, round=None, appraisal=None):
    approvals = AppraisalApproval.objects.filter(kind=kind, status=ApprovalStatus.PENDING, is_deleted=False)
    if appraisal is not None:
        return approvals.filter(appraisal=appraisal).first()
    return approvals.filter(programme=programme, round=round).first()


def check_not_pending(kind, programme=None, round=None, appraisal=None):
    if pending_approval(kind, programme, round, appraisal):
        raise ValidationError(f"appraisal.approval_pending: a {Kind(kind).label.lower()} is awaiting approval")


def request_approval(user, kind, programme, round, proposal, summary, appraisal=None):
    """Keep the change and open its task; returns the AppraisalApproval."""
    from tasks_management.apps import TasksManagementConfig
    from tasks_management.models import Task
    from tasks_management.services import TaskService

    approval = AppraisalApproval(kind=kind, programme=programme, round=round, appraisal=appraisal,
                                 proposal=proposal, summary=summary, requested_by=user, requested_at=timezone.now())
    approval.save(user=user)
    result = TaskService(user).create({
        "source": TASK_SOURCE,
        "entity": approval,
        "status": Task.Status.RECEIVED,
        "executor_action_event": TasksManagementConfig.default_executor_event,
        "business_event": BUSINESS_EVENTS[kind],
        "json_ext": {"kind": kind, "programme": programme, "round": round, "requested_by": user.username, **summary},
    })
    if not result or not result.get("success"):
        raise ValidationError("appraisal.approval_not_started: "
                              f"{(result or {}).get('detail') or (result or {}).get('message') or 'no task'}")
    approval.task_id = result["data"]["id"]
    approval.save(user=user)
    return approval


def _appliers():
    from appraisal.review import apply_decision_approval
    from appraisal.scoring import apply_shortlist_approval

    return {Kind.SHORTLIST: apply_shortlist_approval, Kind.ELIGIBILITY: apply_decision_approval}


def on_task_complete(**kwargs):
    """tasks_management complete_task (after): apply an approved change, or close a rejected one."""
    from core.models import User
    from tasks_management.models import Task

    result = kwargs.get("result") or {}
    if not result.get("success"):
        return
    data = result.get("data") or {}
    task = data.get("task") or {}
    if task.get("business_event") not in BUSINESS_EVENTS.values():
        return
    approval = AppraisalApproval.objects.filter(task_id=task.get("id"), status=ApprovalStatus.PENDING,
                                                is_deleted=False).first()
    if approval is None:
        return
    user = User.objects.get(id=data["user"]["id"])
    approval.decided_by, approval.decided_at = user, timezone.now()
    if task.get("status") != Task.Status.COMPLETED:
        approval.status = ApprovalStatus.REJECTED
        approval.save(user=user)
        return
    from appraisal.apps import AppraisalConfig

    perms = AppraisalConfig.gql_mutation_approve_appraisal_perms
    try:
        if not perms or not user.has_perms(perms):
            raise ValidationError("appraisal.approver_not_allowed: the approver lacks the right to approve appraisals")
        # A savepoint, so a change that can't be applied leaves the appraisals as they were
        with transaction.atomic():
            _appliers()[approval.kind](approval, user)
        approval.status = ApprovalStatus.APPROVED
    except Exception as exc:
        approval.status = ApprovalStatus.FAILED
        approval.outcome = "; ".join(getattr(exc, "messages", None) or [str(exc)])
        Task.objects.filter(id=task["id"]).update(status=Task.Status.FAILED)
        logger.warning("Appraisal approval %s could not be applied: %s", approval.id, approval.outcome)
    approval.save(user=user)
