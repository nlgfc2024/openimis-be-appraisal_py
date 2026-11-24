from decimal import Decimal

import graphene
from graphene_django import DjangoObjectType

from core import ExtendedConnection
from appraisal.models import Appraisal, AppraisalItem, AppraisalReturn, DocumentReview

# Bookkeeping fields left out of the change history
_UNTRACKED = {"version", "date_updated", "user_updated", "date_created", "user_created", "id"}
HISTORY_LIMIT = 200


def _rules(appraisal, kind):
    """{key: rule} of the appraisal's checklist (ELIGIBILITY) or score components (SCORE), in their order."""
    snapshot = appraisal.config_snapshot or {}
    rules = snapshot.get("eligibility", []) if kind == AppraisalItem.Kind.ELIGIBILITY else \
        (snapshot.get("scoring") or {}).get("components", [])
    return {rule["key"]: rule for rule in rules}


class AppraisalItemGQLType(DjangoObjectType):
    uuid = graphene.String(source="uuid")
    label = graphene.String()
    result = graphene.String(description="The result that counts: the officer's when given, else the system's")
    points = graphene.Decimal(description="The points that count: the officer's when given, else the system's")
    is_overridden = graphene.Boolean()
    basis = graphene.JSONString()
    decided_by = graphene.String(description="Username of the officer who answered")
    max_points = graphene.Decimal(description="The most points a score item can give")
    officer_value = graphene.String(description="A score item's value as an officer corrected it")

    class Meta:
        model = AppraisalItem
        interfaces = (graphene.relay.Node,)
        fields = ("id", "kind", "key", "system_result", "system_points", "basis_changed", "officer_result",
                  "officer_points", "evidence", "comment", "decided_at", "date_updated")
        filter_fields = {"kind": ["exact"], "key": ["exact"]}
        connection_class = ExtendedConnection

    def resolve_label(self, info):
        rule = _rules(self.appraisal, self.kind).get(self.key) or {}
        return rule.get("label") or self.key

    def resolve_result(self, info):
        return self.result

    def resolve_points(self, info):
        return self.points

    def resolve_is_overridden(self, info):
        return self.is_overridden

    def resolve_basis(self, info):
        return self.basis

    def resolve_decided_by(self, info):
        return self.decided_by.username if self.decided_by_id else None

    def resolve_officer_value(self, info):
        from appraisal.scoring import OFFICER_VALUE

        return (self.json_ext or {}).get(OFFICER_VALUE)

    def resolve_max_points(self, info):
        if self.kind != AppraisalItem.Kind.SCORE:
            return None
        from enrolment.ranking import maximum_points

        rule = _rules(self.appraisal, self.kind).get(self.key)
        return Decimal(str(maximum_points(rule["scoring"]))) if rule else None


class DocumentCheckGQLType(graphene.ObjectType):
    """One document of the document check: uploaded (with its review) or required and missing (OUTSTANDING)."""
    document_type = graphene.String()
    label = graphene.String()
    required = graphene.Boolean()
    document_id = graphene.UUID(description="The msr_etl ApplicantDocument; null when outstanding")
    filename = graphene.String()
    download_status = graphene.String(description="msr_etl's PENDING / STORED / FAILED")
    status = graphene.String(description="SUBMITTED, VERIFIED, REJECTED or OUTSTANDING")
    comment = graphene.String()
    reviewed_by = graphene.String()
    reviewed_at = graphene.DateTime()


class AppraisalReturnGQLType(DjangoObjectType):
    uuid = graphene.String(source="uuid")
    requested_by = graphene.String()
    resubmitted_by = graphene.String()
    is_open = graphene.Boolean()

    class Meta:
        model = AppraisalReturn
        fields = ("id", "stage", "previous_status", "reason", "action_required", "returned_at", "resubmitted_at",
                  "resubmission_note")

    def resolve_requested_by(self, info):
        return self.requested_by.username

    def resolve_resubmitted_by(self, info):
        return self.resubmitted_by.username if self.resubmitted_by_id else None

    def resolve_is_open(self, info):
        return self.is_open


class AppraisalChangeGQLType(graphene.ObjectType):
    date = graphene.DateTime()
    user = graphene.String()
    subject = graphene.String(description="'appraisal', or the key of the item changed")
    changes = graphene.JSONString(description="[{field, old, new}]; empty for the creation")


class DecisionPreviewGQLType(graphene.ObjectType):
    """What the checklist leads to now, by the rule decideAppraisalEligibility applies."""
    decision = graphene.String(description="ELIGIBLE, INELIGIBLE or CLARIFICATION; null while an item is unanswered")
    ineligible = graphene.List(graphene.String, description="Keys of the items answered No")
    pending = graphene.List(graphene.String, description="Keys of the items answered Pending")
    unanswered = graphene.List(graphene.String, description="Keys of the rules with no answer yet")
    changed = graphene.List(graphene.String, description="Keys whose application changed since an officer answered")
    problem = graphene.String(description="Why the decision can't be recorded now (the mutation's refusal), or null")


def _allowed(info, perms_name):
    """Whether the requesting user holds the appraisal right configured as ``perms_name``."""
    from appraisal.apps import AppraisalConfig

    perms = getattr(AppraisalConfig, perms_name)
    user = info.context.user
    return bool(perms) and user.is_authenticated and user.has_perms(perms)


def _can_screen(appraisal, info):
    from appraisal.review import screening_problem

    return _allowed(info, "gql_mutation_screen_appraisal_perms") and screening_problem(appraisal) is None


class AppraisalGQLType(DjangoObjectType):
    uuid = graphene.String(source="uuid")
    decision_preview = graphene.Field(DecisionPreviewGQLType)
    can_screen = graphene.Boolean(description="The user may answer the checklist now")
    can_decide = graphene.Boolean(description="The user may record the eligibility decision now")
    can_reopen = graphene.Boolean(description="The user may reopen the recorded decision")
    can_score = graphene.Boolean(description="The user may correct a score item's value now")
    can_rescore = graphene.Boolean(description="The user may (re)score the appraisal now")
    can_return = graphene.Boolean(description="The user may return the appraisal for action now")
    can_resubmit = graphene.Boolean(description="The user may resubmit the returned appraisal")
    shortlist_group = graphene.String(description="The quota the last shortlist ranked it in (e.g. its city), or null")
    items = graphene.List(AppraisalItemGQLType, kind=graphene.String(),
                          description="Active items, in the order of the appraisal's rules")
    history = graphene.List(AppraisalChangeGQLType, description="Changes to the appraisal and its items, newest first")
    submission_id = graphene.UUID(description="The msr_etl KoboSubmission appraised")
    kobo_id = graphene.Int()
    json_ext = graphene.JSONString()
    documents = graphene.List(DocumentCheckGQLType, description="The document check")
    documents_complete = graphene.Boolean(description="Every required document verified and none rejected")
    returns = graphene.List(AppraisalReturnGQLType, description="Returns for action, newest first")

    class Meta:
        model = Appraisal
        interfaces = (graphene.relay.Node,)
        fields = ("id", "individual", "programme", "round", "stage", "status", "eligibility", "score", "rank",
                  "date_created", "date_updated", "version")
        filter_fields = {
            "id": ["exact"],
            "programme": ["exact"],
            "round": ["exact"],
            "stage": ["exact", "in"],
            "status": ["exact", "in"],
            "eligibility": ["exact", "isnull"],
            "individual__id": ["exact"],
        }
        connection_class = ExtendedConnection

    def resolve_items(self, info, kind=None):
        items = [item for item in self.items.all() if not item.is_deleted and (kind is None or item.kind == kind)]
        order = {(k, key): i for k in AppraisalItem.Kind.values for i, key in enumerate(_rules(self, k))}
        return sorted(items, key=lambda item: (item.kind, order.get((item.kind, item.key), len(order)), item.key))

    def resolve_decision_preview(self, info):
        from appraisal.review import decision_preview

        return decision_preview(self)

    def resolve_can_screen(self, info):
        return _can_screen(self, info)

    def resolve_can_decide(self, info):
        from appraisal.review import decision_preview

        return _can_screen(self, info) and decision_preview(self)["problem"] is None

    def resolve_can_score(self, info):
        return _allowed(info, "gql_mutation_score_appraisal_perms") and self.status == Appraisal.Status.SCORED

    def resolve_can_rescore(self, info):
        from appraisal.scoring import SCORABLE

        return _allowed(info, "gql_mutation_score_appraisal_perms") and self.status in SCORABLE

    def resolve_can_return(self, info):
        from appraisal.returns import return_problem

        return _allowed(info, "gql_mutation_return_appraisal_perms") and return_problem(self) is None

    def resolve_can_resubmit(self, info):
        from appraisal.returns import can_resubmit

        return _allowed(info, "gql_mutation_return_appraisal_perms") and can_resubmit(self)

    def resolve_shortlist_group(self, info):
        from appraisal.scoring import SHORTLIST_GROUP

        return (self.json_ext or {}).get(SHORTLIST_GROUP)

    def resolve_can_reopen(self, info):
        from appraisal.review import REOPENABLE

        return _allowed(info, "gql_mutation_return_appraisal_perms") and self.status in REOPENABLE

    def resolve_submission_id(self, info):
        return self.submission_id

    def resolve_kobo_id(self, info):
        return self.submission.kobo_id if self.submission_id else None

    def resolve_json_ext(self, info):
        return self.json_ext

    def resolve_documents(self, info):
        from appraisal.documents import document_check

        rows = []
        for entry in document_check(self):
            document, review = entry["document"], entry["review"]
            rows.append(DocumentCheckGQLType(
                document_type=entry["document_type"], label=entry["label"], required=entry["required"],
                document_id=document.id if document else None, filename=document.filename if document else None,
                download_status=document.status if document else None, status=entry["status"],
                comment=review.comment if review else None,
                reviewed_by=review.reviewed_by.username if review else None,
                reviewed_at=review.reviewed_at if review else None))
        return rows

    def resolve_documents_complete(self, info):
        from appraisal.documents import documents_problems

        return not documents_problems(self)

    def resolve_returns(self, info):
        return self.returns.filter(is_deleted=False).select_related("requested_by", "resubmitted_by") \
            .order_by("-returned_at")

    def resolve_history(self, info):
        entries = _changes(self.history.select_related("user_updated").order_by("history_date"),
                           lambda record: "appraisal")
        item_history = AppraisalItem.history.filter(appraisal_id=self.id).select_related("user_updated") \
            .order_by("id", "history_date")
        entries += _changes(item_history, lambda record: record.key)
        reviews = DocumentReview.history.filter(appraisal_id=self.id).select_related("user_updated", "document") \
            .order_by("id", "history_date")
        entries += _changes(reviews, lambda record: f"document:{record.document.document_type}")
        returns = AppraisalReturn.history.filter(appraisal_id=self.id).select_related("user_updated") \
            .order_by("id", "history_date")
        entries += _changes(returns, lambda record: "return")
        return sorted(entries, key=lambda entry: entry["date"], reverse=True)[:HISTORY_LIMIT]


def _changes(records, subject):
    """[{date, user, subject, changes}] from simple_history records, each against the previous of its object."""
    entries, previous = [], {}
    for record in records:
        before = previous.get(record.id)
        changes = [] if before is None else [
            {"field": change.field, "old": _plain(change.old), "new": _plain(change.new)}
            for change in record.diff_against(before).changes if change.field not in _UNTRACKED]
        previous[record.id] = record
        if before is not None and not changes:
            continue
        entries.append({"date": record.history_date, "subject": subject(record), "changes": changes,
                        "user": record.user_updated.username if record.user_updated_id else None})
    return entries


def _plain(value):
    return value if value is None or isinstance(value, (str, int, float, bool, dict, list)) else str(value)


class AppraisalRoundGQLType(graphene.ObjectType):
    """A programme round: the one configured now, or one with appraisals the user may see."""
    programme = graphene.String()
    round = graphene.String()
    configured = graphene.Boolean(description="The programme's current round in the configuration")
    appraisals = graphene.Int(description="Appraisals of the round the user may see")
