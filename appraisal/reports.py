"""The desk appraisal report of a programme round (Operations Manual 8.12), over the appraisals the user may see.

- Counts by status and stage, by city (the shortlist's location type) with the outcome in each, and by each
  breakdown the programme configures in ``report.breakdowns`` (an application field, e.g. the sector).
- The document check of the appraisals still being screened: how many miss a required document, have documents to
  review or rejected ones.
- What awaits action: applications not started, documents to review, clarifications asked, answers changed under
  an officer, appraisals returned for action, and changes awaiting approval.
"""
from collections import Counter

from django.db.models import Count, Q

from appraisal.models import Appraisal, AppraisalApproval, AppraisalItem, DocumentReview
from appraisal.review import OPEN_STATUSES
from appraisal.scoring import _city_finder

Status, Eligibility = Appraisal.Status, Appraisal.Eligibility
DocumentStatus = DocumentReview.Status
SCREENED = (Status.RECEIVED, Status.DOCUMENT_CHECK, Status.DESK_SCREENING, Status.RETURNED)
NO_VALUE = ""


def _counts(counter):
    return [{"key": key, "count": count} for key, count in sorted(counter.items(), key=lambda item: -item[1])]


def _document_check(appraisals):
    """{"incomplete", "outstanding", "to_review", "rejected"}: appraisals still being screened whose required
    documents are missing, or with documents not reviewed yet or rejected."""
    from msr_etl.models import ApplicantDocument

    from appraisal.documents import required_types

    screened = [appraisal for appraisal in appraisals
                if appraisal.status in SCREENED and (appraisal.config_snapshot or {}).get("documents") is not None]
    types, uploaded = {}, Counter()
    for document_type, submission_id in ApplicantDocument.objects.filter(
            submission_id__in=[appraisal.submission_id for appraisal in screened if appraisal.submission_id]) \
            .values_list("document_type", "submission_id"):
        types.setdefault(submission_id, set()).add(document_type)
        uploaded[submission_id] += 1
    reviews = {}
    decided = DocumentReview.objects.filter(appraisal__in=screened, is_deleted=False) \
        .values_list("appraisal_id", "document__document_type", "status")
    for appraisal_id, document_type, status in decided:
        reviews.setdefault(appraisal_id, []).append((document_type, status))
    result = Counter()
    for appraisal in screened:
        present, count = types.get(appraisal.submission_id, set()), uploaded[appraisal.submission_id]
        decided = reviews.get(appraisal.id, [])
        verified = {kind for kind, status in decided if status == DocumentStatus.VERIFIED}
        rejected = any(status == DocumentStatus.REJECTED for _kind, status in decided)
        required = required_types(appraisal)
        missing = [kind for kind in required if kind not in present]
        result["outstanding"] += bool(missing)
        result["to_review"] += count > len(decided)
        result["rejected"] += rejected
        # As the eligibility decision sees it (documents.documents_problems)
        result["incomplete"] += bool(rejected or any(kind not in verified for kind in required))
    return {key: result[key] for key in ("incomplete", "outstanding", "to_review", "rejected")}


def appraisal_report(user, programme, round=None):
    """The report of ``programme``'s ``round`` (its latest when None); None when it has no appraisals."""
    appraisals = Appraisal.get_queryset(Appraisal.objects.filter(programme=programme, is_deleted=False), user)
    if round is None:
        latest = appraisals.order_by("-date_created").values_list("round", flat=True).first()
        if latest is None:
            return None
        round = latest
    appraisals = list(appraisals.filter(round=round).select_related("individual__location"))
    if not appraisals:
        return None
    snapshot = appraisals[-1].config_snapshot or {}
    shortlist = snapshot.get("shortlist") or {}
    city_of = _city_finder(shortlist)
    places = shortlist.get("per_city") or {}

    cities = {}
    for appraisal in appraisals:
        city = city_of(appraisal) or NO_VALUE
        row = cities.setdefault(city, Counter())
        row["total"] += 1
        row["eligible"] += appraisal.eligibility == Eligibility.ELIGIBLE
        row["ineligible"] += appraisal.eligibility == Eligibility.INELIGIBLE
        row["clarification"] += appraisal.eligibility == Eligibility.CLARIFICATION
        row["scored"] += appraisal.score is not None
        row["shortlisted"] += appraisal.status == Status.SHORTLISTED

    breakdowns = []
    for rule in (snapshot.get("report") or {}).get("breakdowns", []):
        values = Counter(str((appraisal.individual.json_ext or {}).get(rule["field"]) or NO_VALUE)
                         for appraisal in appraisals)
        breakdowns.append({"key": rule["key"], "label": rule["label"], "counts": _counts(values)})

    ids = [appraisal.id for appraisal in appraisals]
    open_ids = [appraisal.id for appraisal in appraisals if appraisal.status not in (Status.INELIGIBLE,
                                                                                     Status.SHORTLISTED,
                                                                                     Status.NOT_SHORTLISTED)]
    documents = _document_check(appraisals)
    approvals = AppraisalApproval.get_queryset(AppraisalApproval.objects.filter(
        programme=programme, round=round, status=AppraisalApproval.Status.PENDING, is_deleted=False), user) \
        .aggregate(shortlist=Count("id", filter=Q(kind=AppraisalApproval.Kind.SHORTLIST)),
                   eligibility=Count("id", filter=Q(kind=AppraisalApproval.Kind.ELIGIBILITY)))
    awaiting = {
        "not_started": sum(appraisal.status == Status.RECEIVED for appraisal in appraisals),
        "documents_to_review": documents["to_review"],
        "documents_outstanding": documents["outstanding"],
        "clarification": sum(appraisal.eligibility == Eligibility.CLARIFICATION
                             and appraisal.status in OPEN_STATUSES for appraisal in appraisals),
        "answers_changed": AppraisalItem.objects.filter(appraisal_id__in=open_ids, basis_changed=True,
                                                        is_deleted=False)
        .values("appraisal_id").distinct().count(),
        "returned": sum(appraisal.status == Status.RETURNED for appraisal in appraisals),
        "eligibility_approval": approvals["eligibility"],
        "shortlist_approval": approvals["shortlist"],
    }
    return {
        "programme": programme,
        "round": round,
        "total": len(ids),
        "by_status": _counts(Counter(appraisal.status for appraisal in appraisals)),
        "by_stage": _counts(Counter(appraisal.stage for appraisal in appraisals)),
        "by_city": [{"city": city, "places": places.get(city), **{key: row[key] for key in (
            "total", "eligible", "ineligible", "clarification", "scored", "shortlisted")}}
            for city, row in sorted(cities.items(), key=lambda item: (item[0] == NO_VALUE, item[0]))],
        "breakdowns": breakdowns,
        "documents": documents,
        "awaiting": awaiting,
    }
