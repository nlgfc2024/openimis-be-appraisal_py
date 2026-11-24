import graphene
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.utils.translation import gettext as _

from core.schema import OrderedDjangoFilterConnectionField
from individual.apps import IndividualConfig
from individual.models import Individual
from individual.schema import Query as IndividualQuery

from appraisal.apps import AppraisalConfig
from appraisal.gql_mutations import (
    AnswerAppraisalItemMutation, DecideAppraisalEligibilityMutation, ReopenAppraisalMutation,
)
from appraisal.gql_queries import AppraisalGQLType
from appraisal.models import Appraisal


def _require_view(user):
    """Appraisals show the applicant's registry record and answers, so viewing them also needs the individual
    search right."""
    if type(user) is AnonymousUser or not user.id:
        raise PermissionDenied(_("unauthorized"))
    for perms in (AppraisalConfig.gql_query_appraisal_perms, IndividualConfig.gql_individual_search_perms):
        if not perms or not user.has_perms(perms):
            raise PermissionDenied(_("unauthorized"))
    AppraisalConfig.check_configured()


def _location_filter(parent_location, parent_location_level):
    """Appraisals of individuals in ``parent_location`` (uuid) at ``parent_location_level``, by individual's rule."""
    located = Individual.objects.filter(IndividualQuery._get_location_filters(parent_location, parent_location_level))
    return Q(individual__in=located)


def _number(text):
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _answer_filter(answers):
    """``key=value`` pairs matched on the applicant's answers (Individual.json_ext), e.g. primary_sector=energy. A
    numeric value also matches an answer stored as a JSON number (jobs_to_create=5)."""
    condition = Q()
    for answer in answers:
        key, _sep, value = answer.partition("=")
        key, value = key.strip(), value.strip()
        if not key or not key.replace("_", "").isalnum():
            raise ValueError(f"Invalid answer filter: {answer}")
        match = Q(**{f"individual__json_ext__{key}": value})
        number = _number(value)
        if number is not None:
            match |= Q(**{f"individual__json_ext__{key}": number})
        condition &= match
    return condition


class Query(graphene.ObjectType):
    appraisal = OrderedDjangoFilterConnectionField(
        AppraisalGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        search=graphene.String(description="Part of the applicant's name or national ID"),
        answers=graphene.List(of_type=graphene.String,
                              description="key=value matched on the applicant's answers, as text or as a number"),
        parent_location=graphene.String(),
        parent_location_level=graphene.Int(),
        needs_review=graphene.Boolean(description="Only appraisals with an item whose basis changed"),
    )

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
    reopen_appraisal = ReopenAppraisalMutation.Field()
