import graphene
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext as _

from core.gql.gql_mutations.base_mutation import BaseMutation
from core.schema import OpenIMISMutation

from appraisal import documents, returns, review, scoring
from appraisal.apps import AppraisalConfig


class ResultEnum(graphene.Enum):
    YES = "YES"
    NO = "NO"
    PENDING = "PENDING"


def _require(user, perms):
    if type(user) is AnonymousUser or not user.id or not perms or not user.has_perms(perms):
        raise PermissionDenied(_("unauthorized"))


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


class ScoreAppraisalsMutation(BaseMutation):
    """Score (or rescore) the eligible appraisals of a programme round, or only ``ids`` (Annex 2B)."""
    _mutation_class = "ScoreAppraisalsMutation"
    _mutation_module = "appraisal"

    class Input(OpenIMISMutation.Input):
        programme = graphene.String(required=True)
        round = graphene.String(required=True)
        ids = graphene.List(graphene.UUID, description="Only these appraisals")

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require(user, AppraisalConfig.gql_mutation_score_appraisal_perms)

    @classmethod
    def _mutate(cls, user, **data):
        scoring.score_round(user, data["programme"], data["round"], data.get("ids"))


class AnswerAppraisalScoreMutation(BaseMutation):
    """An officer's points for one score component."""
    _mutation_class = "AnswerAppraisalScoreMutation"
    _mutation_module = "appraisal"

    class Input(OpenIMISMutation.Input):
        id = graphene.UUID(required=True, description="The score item")
        points = graphene.Decimal(required=True)
        evidence = graphene.String(required=True, description="Evidence or source checked")
        comment = graphene.String()

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require(user, AppraisalConfig.gql_mutation_score_appraisal_perms)

    @classmethod
    def _mutate(cls, user, **data):
        scoring.answer_score(user, data["id"], data["points"], data.get("evidence", ""), data.get("comment", ""))


class ShortlistAppraisalsMutation(BaseMutation):
    """Rank a programme round's scored appraisals and shortlist the top places."""
    _mutation_class = "ShortlistAppraisalsMutation"
    _mutation_module = "appraisal"

    class Input(OpenIMISMutation.Input):
        programme = graphene.String(required=True)
        round = graphene.String(required=True)

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require(user, AppraisalConfig.gql_mutation_shortlist_appraisal_perms)

    @classmethod
    def _mutate(cls, user, **data):
        scoring.shortlist_round(user, data["programme"], data["round"])


class DocumentStatusEnum(graphene.Enum):
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class ReviewApplicantDocumentMutation(BaseMutation):
    """An officer's VERIFIED or REJECTED for one document of the appraised application."""
    _mutation_class = "ReviewApplicantDocumentMutation"
    _mutation_module = "appraisal"

    class Input(OpenIMISMutation.Input):
        appraisal_id = graphene.UUID(required=True)
        document_id = graphene.UUID(required=True)
        status = graphene.Field(DocumentStatusEnum, required=True)
        comment = graphene.String(description="Required for REJECTED: why")

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require(user, AppraisalConfig.gql_mutation_review_document_perms)

    @classmethod
    def _mutate(cls, user, **data):
        status = getattr(data["status"], "value", data["status"])
        documents.review_document(user, data["appraisal_id"], data["document_id"], status, data.get("comment", ""))


class ReturnAppraisalMutation(BaseMutation):
    """Return an appraisal for action, with the reason and the action required."""
    _mutation_class = "ReturnAppraisalMutation"
    _mutation_module = "appraisal"

    class Input(OpenIMISMutation.Input):
        id = graphene.UUID(required=True)
        reason = graphene.String(required=True)
        action_required = graphene.String(required=True)

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require(user, AppraisalConfig.gql_mutation_return_appraisal_perms)

    @classmethod
    def _mutate(cls, user, **data):
        returns.return_appraisal(user, data["id"], data["reason"], data["action_required"])


class ResubmitAppraisalMutation(BaseMutation):
    """Close a returned appraisal's return and restore its status."""
    _mutation_class = "ResubmitAppraisalMutation"
    _mutation_module = "appraisal"

    class Input(OpenIMISMutation.Input):
        id = graphene.UUID(required=True)
        note = graphene.String()

    @classmethod
    def _validate_mutation(cls, user, **data):
        _require(user, AppraisalConfig.gql_mutation_return_appraisal_perms)

    @classmethod
    def _mutate(cls, user, **data):
        returns.resubmit_appraisal(user, data["id"], data.get("note", ""))


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
