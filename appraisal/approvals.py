"""Supervisor approval (Operations Manual 8.12) through tasks_management's maker-checker.

- A programme sets ``approval.shortlist`` and/or ``approval.eligibility``. The officer's change is then kept as an
  AppraisalApproval with a task in tasks_management, and nothing changes on the appraisals until it is approved.
- The task is approved or rejected in the Tasks screen by its task group (source ``appraisal``), under the group's
  completion policy. It is a shared task, belonging to no benefit plan. Approving needs
  ``gql_mutation_approve_appraisal_perms`` and someone other than the officer who asked: otherwise the completion is
  refused before the task changes, and the task stays open for someone who may approve it. On
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

    approval = AppraisalApproval(kind=kind, programme=programme, round=round, appraisal=appraisal,
                                 proposal=proposal, summary=summary, requested_by=user, requested_at=timezone.now())
    approval.save(user=user)
    task = {
        "source": TASK_SOURCE,
        "entity": approval,
        "status": Task.Status.RECEIVED,
        "executor_action_event": TasksManagementConfig.default_executor_event,
        "business_event": BUSINESS_EVENTS[kind],
        "json_ext": {"kind": kind, "programme": programme, "round": round, "requested_by": user.username, **summary},
    }
    result = _task_service(user, task).create(task)
    if not result or not result.get("success"):
        raise ValidationError("appraisal.approval_not_started: "
                              f"{(result or {}).get('detail') or (result or {}).get('message') or 'no task'}")
    approval.task_id = result["data"]["id"]
    approval.save(user=user)
    return approval


def _task_service(user, task):
    """A TaskService that opens ``task`` as a shared task: appraisals belong to no benefit plan, so with benefit-plan
    access control on, plan-restricted approvers still see it. tasks_management versions before shared tasks
    (no ``trusted_internal``) open a plain task."""
    import inspect

    from tasks_management.services import TaskService

    if "trusted_internal" in inspect.signature(TaskService.__init__).parameters:
        task["benefit_plan_is_shared"] = True
        return TaskService(user, trusted_internal=True)
    return TaskService(user)


def _approval_problem(approval, user):
    """Why ``user`` may not approve ``approval``, or None."""
    from appraisal.apps import AppraisalConfig

    perms = AppraisalConfig.gql_mutation_approve_appraisal_perms
    if not perms or not user.has_perms(perms):
        return "appraisal.approver_not_allowed: the approver lacks the right to approve appraisals"
    if approval.requested_by_id == user.id:
        return "appraisal.own_change: a change can't be approved by the officer who asked for it"
    return None


def authorize_task_completion(**kwargs):
    """tasks_management complete_task (before): refuse to approve an appraisal change the user may not approve, so
    the task stays open for someone who may. Rejecting is left to the task group."""
    from django.core.exceptions import PermissionDenied

    signal_data = kwargs.get("data") or []
    positional = signal_data[0] if signal_data else ()
    obj_data = positional[0] if positional else None
    if not isinstance(obj_data, dict) or not obj_data.get("id") or obj_data.get("failed"):
        return
    approval = AppraisalApproval.objects.filter(task_id=obj_data["id"], status=ApprovalStatus.PENDING,
                                                is_deleted=False).first()
    user = getattr(kwargs.get("cls_"), "user", None)
    if approval is None or user is None:
        return
    problem = _approval_problem(approval, user)
    if problem:
        raise PermissionDenied(problem)


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
    try:
        # Checked before completion too (authorize_task_completion); kept here for a completion that skipped it
        problem = _approval_problem(approval, user)
        if problem:
            raise ValidationError(problem)
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
