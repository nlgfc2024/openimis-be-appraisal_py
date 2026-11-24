import graphene
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext as _

from core.gql.gql_mutations.base_mutation import BaseMutation
from core.schema import OpenIMISMutation

from appraisal import review
from appraisal.apps import AppraisalConfig


class ResultEnum(graphene.Enum):
    YES = "YES"
    NO = "NO"
    PENDING = "PENDING"


def _require(user, perms):
    if type(user) is AnonymousUser or not user.id or not perms or not user.has_perms(perms):
        raise PermissionDenied(_("unauthorized"))
    AppraisalConfig.check_configured()


class AnswerAppraisalItemMutation(BaseMutation):
    """An officer's answer to one eligibility checklist item."""
    _mutation_class = "AnswerAppraisalItemMutation"
    _mutation_module = "appraisal"

    class Input(OpenIMISMutation.Input):
        id = graphene.UUID(required=True, description="The appraisal item")
        result = graphene.Field(ResultEnum, required=True)
        evidence = graphene.String(description="Evidence or source checked; required for YES and NO")
        comment = graphene.String(description="Comment; for PENDING, the clarification needed")

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require(user, AppraisalConfig.gql_mutation_screen_appraisal_perms)

    @classmethod
    def _mutate(cls, user, **data):
        result = getattr(data["result"], "value", data["result"])
        review.answer_item(user, data["id"], result, data.get("evidence", ""), data.get("comment", ""))


class DecideAppraisalEligibilityMutation(BaseMutation):
    """The eligibility decision from an appraisal's checklist (Annex 2A)."""
    _mutation_class = "DecideAppraisalEligibilityMutation"
    _mutation_module = "appraisal"

    class Input(OpenIMISMutation.Input):
        id = graphene.UUID(required=True, description="The appraisal")
        comment = graphene.String()

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require(user, AppraisalConfig.gql_mutation_screen_appraisal_perms)

    @classmethod
    def _mutate(cls, user, **data):
        review.decide_eligibility(user, data["id"], data.get("comment", ""))


class ReopenAppraisalMutation(BaseMutation):
    """Reopen a decided screening to correct its eligibility decision, with the reason."""
    _mutation_class = "ReopenAppraisalMutation"
    _mutation_module = "appraisal"

    class Input(OpenIMISMutation.Input):
        id = graphene.UUID(required=True, description="The appraisal")
        reason = graphene.String(required=True, description="Why the decision is being corrected")

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require(user, AppraisalConfig.gql_mutation_return_appraisal_perms)

    @classmethod
    def _mutate(cls, user, **data):
        review.reopen_screening(user, data["id"], data["reason"])
