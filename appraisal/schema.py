import graphene
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.utils.translation import gettext as _

from core.schema import OrderedDjangoFilterConnectionField
from location.apps import LocationConfig

from appraisal.apps import AppraisalConfig
from appraisal.gql_mutations import (
    AnswerAppraisalItemMutation,
    AnswerAppraisalScoreMutation,
    DecideAppraisalEligibilityMutation,
    ResubmitAppraisalMutation,
    ReturnAppraisalMutation,
    ReviewApplicantDocumentMutation,
    ScoreAppraisalsMutation,
    ShortlistAppraisalsMutation,
)
from appraisal.gql_queries import AppraisalApprovalGQLType, AppraisalGQLType, AppraisalReportGQLType
from appraisal.models import Appraisal, AppraisalApproval


def _location_filter(parent_location, parent_location_level):
    """Individuals in the location ``parent_location`` (uuid) at ``parent_location_level`` (0 for the top)."""
    key = "uuid"
    for _level in range(len(LocationConfig.location_types) - parent_location_level - 1):
        key = "parent__" + key
    return Q(**{f"individual__location__{key}": parent_location})


def _answer_filter(answers):
    """``key=value`` pairs matched on the applicant's answers (Individual.json_ext), e.g. primary_sector=energy."""
    condition = Q()
    for answer in answers:
        key, _sep, value = answer.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            raise ValueError(f"Invalid answer filter: {answer}")
        condition &= Q(**{f"individual__json_ext__{key}": value.strip()})
    return condition


APPROVALS_LIMIT = 20


def _require_view(user):
    perms = AppraisalConfig.gql_query_appraisal_perms
    if type(user) is AnonymousUser or not user.id or not perms or not user.has_perms(perms):
        raise PermissionDenied(_("unauthorized"))


class Query(graphene.ObjectType):
    appraisal = OrderedDjangoFilterConnectionField(
        AppraisalGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        search=graphene.String(description="Part of the applicant's name or national ID"),
        answers=graphene.List(of_type=graphene.String, description="key=value matched on the applicant's answers"),
        parent_location=graphene.String(),
        parent_location_level=graphene.Int(),
        needs_review=graphene.Boolean(description="Only appraisals with an item whose basis changed"),
    )

    appraisal_approvals = graphene.List(
        AppraisalApprovalGQLType, programme=graphene.String(required=True), round=graphene.String(),
        kind=graphene.String(), description="Approvals of a programme round, newest first",
    )
    appraisal_report = graphene.Field(
        AppraisalReportGQLType, programme=graphene.String(required=True),
        round=graphene.String(description="The latest round when not given"),
    )

    def resolve_appraisal_approvals(self, info, programme, round=None, kind=None):
        user = info.context.user
        _require_view(user)
        approvals = AppraisalApproval.objects.filter(programme=programme, is_deleted=False)
        if round:
            approvals = approvals.filter(round=round)
        if kind:
            approvals = approvals.filter(kind=kind)
        return AppraisalApproval.get_queryset(approvals, user).select_related("requested_by", "decided_by") \
            .order_by("-requested_at")[:APPROVALS_LIMIT]

    def resolve_appraisal_report(self, info, programme, round=None):
        from appraisal.reports import appraisal_report

        user = info.context.user
        _require_view(user)
        return appraisal_report(user, programme, round)

    def resolve_appraisal(self, info, **kwargs):
        user = info.context.user
        _require_view(user)
        filters = [Q(is_deleted=False)]
        search = (kwargs.get("search") or "").strip()
        if search:
            filters.append(Q(individual__first_name__icontains=search) | Q(individual__last_name__icontains=search)
                           | Q(individual__json_ext__national_id__icontains=search))
        if kwargs.get("answers"):
            filters.append(_answer_filter(kwargs["answers"]))
        if kwargs.get("parent_location") is not None and kwargs.get("parent_location_level") is not None:
            filters.append(_location_filter(kwargs["parent_location"], kwargs["parent_location_level"]))
        if kwargs.get("needs_review"):
            filters.append(Q(items__basis_changed=True, items__is_deleted=False))
        queryset = Appraisal.objects.filter(*filters).distinct()
        return Appraisal.get_queryset(queryset, user).select_related("individual", "submission")


class Mutation(graphene.ObjectType):
    answer_appraisal_item = AnswerAppraisalItemMutation.Field()
    decide_appraisal_eligibility = DecideAppraisalEligibilityMutation.Field()
    score_appraisals = ScoreAppraisalsMutation.Field()
    answer_appraisal_score = AnswerAppraisalScoreMutation.Field()
    shortlist_appraisals = ShortlistAppraisalsMutation.Field()
    review_applicant_document = ReviewApplicantDocumentMutation.Field()
    return_appraisal = ReturnAppraisalMutation.Field()
    resubmit_appraisal = ResubmitAppraisalMutation.Field()
