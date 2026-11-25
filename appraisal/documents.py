"""The document check (Operations Manual 8.1d): every document the application uploaded, and every document the
programme requires, with its review status.

- A programme's ``documents.required`` lists document types (the Kobo file question names). An entry may be
  ``{"type": ..., "when": {"field": ..., "in": [...]}}``: required only when that answer is one of the values (a
  multiple-choice answer when any of its choices is).
- An uploaded document is SUBMITTED until an officer marks it VERIFIED or REJECTED (a rejection needs the reason);
  a required document the application lacks is OUTSTANDING.
- The check is complete when every required document is VERIFIED and none is REJECTED. Until then an eligibility
  decision is refused. A programme without ``documents`` has no document check.
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from appraisal.models import Appraisal, DocumentReview

Status = DocumentReview.Status
OPEN_STATUSES = (Appraisal.Status.RECEIVED, Appraisal.Status.DOCUMENT_CHECK, Appraisal.Status.DESK_SCREENING)


def _rules(appraisal):
    return (appraisal.config_snapshot or {}).get("documents")


def _answers(appraisal):
    from msr_etl import kobo_review

    return kobo_review.answers(appraisal.submission) if appraisal.submission_id else {}


def required_types(appraisal):
    """The document types this application must provide, in configured order."""
    rules = _rules(appraisal) or {}
    answers, required = None, []
    for entry in rules.get("required", []):
        if isinstance(entry, str):
            required.append(entry)
            continue
        when = entry.get("when")
        if when:
            answers = _answers(appraisal) if answers is None else answers
            given = str(answers.get(when["field"]) or "").split()
            if not set(given) & {str(value) for value in when["in"]}:
                continue
        required.append(entry["type"])
    return required


def _labels(appraisal):
    """{document type: the form's question label} for the appraised application."""
    if not appraisal.submission_id:
        return {}
    from msr_etl.kobo_form import form_fields, form_for_submission

    form = form_for_submission(appraisal.submission)
    return {field["name"]: field["label"] for field in form_fields(form)} if form else {}


def document_check(appraisal):
    """[{document_type, label, required, document, review, status}]: the required documents in configured order
    (OUTSTANDING when missing), then the application's other documents."""
    from msr_etl.models import ApplicantDocument

    documents = list(ApplicantDocument.objects.filter(submission_id=appraisal.submission_id)
                     .order_by("question", "created_at")) if appraisal.submission_id else []
    reviews = {review.document_id: review
               for review in appraisal.document_reviews.filter(is_deleted=False).select_related("reviewed_by")}
    required, labels = required_types(appraisal), _labels(appraisal)
    entries = [{"document_type": document.document_type, "label": labels.get(document.document_type) or
                document.document_type, "required": document.document_type in required, "document": document,
                "review": reviews.get(document.id),
                "status": reviews[document.id].status if document.id in reviews else Status.SUBMITTED}
               for document in documents]
    present = {document.document_type for document in documents}
    entries += [{"document_type": kind, "label": labels.get(kind) or kind, "required": True, "document": None,
                 "review": None, "status": Status.OUTSTANDING} for kind in required if kind not in present]
    # Required documents first, in the configured order; then the others as the form asks them
    order = {kind: index for index, kind in enumerate(required)}
    return sorted(entries, key=lambda entry: order.get(entry["document_type"], len(order)))


def documents_problems(appraisal):
    """What keeps the document check open: [(label, status)] of required documents not verified and rejected
    documents; empty when complete or when the programme has no document check."""
    if _rules(appraisal) is None:
        return []
    return [(entry["label"], entry["status"]) for entry in document_check(appraisal)
            if entry["status"] == Status.REJECTED or (entry["required"] and entry["status"] != Status.VERIFIED)]


def review_problem(appraisal):
    """Why the appraisal's documents can't be reviewed now, or None (the rule review_document applies)."""
    if appraisal.status not in OPEN_STATUSES:
        return f"appraisal.screening_closed: the appraisal is {appraisal.get_status_display().lower()}"
    return None


@transaction.atomic
def review_document(user, appraisal_id, document_id, status, comment=""):
    """Record an officer's VERIFIED or REJECTED for one document of the appraised application; returns the review."""
    appraisal = Appraisal.get_queryset(Appraisal.objects.filter(id=appraisal_id, is_deleted=False), user) \
        .select_for_update(of=("self",)).first()
    if appraisal is None:
        raise ValidationError("appraisal.not_found")
    problem = review_problem(appraisal)
    if problem:
        raise ValidationError(problem)
    if status not in (Status.VERIFIED, Status.REJECTED):
        raise ValidationError(f"appraisal.invalid_document_status: {status}")
    comment = (comment or "").strip()
    if status == Status.REJECTED and not comment:
        raise ValidationError("appraisal.rejection_reason_required: say why the document is rejected")
    from msr_etl.models import ApplicantDocument

    document = ApplicantDocument.objects.filter(id=document_id, submission_id=appraisal.submission_id).first()
    if document is None:
        raise ValidationError("appraisal.document_not_found: not a document of the appraised application")
    # A document still downloading, or that failed to download, has no file anyone could have opened
    if status == Status.VERIFIED and document.status != ApplicantDocument.Status.STORED:
        status_name = document.get_status_display().lower()
        raise ValidationError(f"appraisal.document_not_stored: the document is {status_name}, "
                              "so it can be rejected but not verified")
    review = appraisal.document_reviews.filter(document=document, is_deleted=False).first() or \
        DocumentReview(appraisal=appraisal, document=document)
    review.status, review.comment, review.reviewed_by, review.reviewed_at = status, comment, user, timezone.now()
    review.save(user=user)
    if appraisal.status == Appraisal.Status.RECEIVED:
        appraisal.status = appraisal.stage = Appraisal.Status.DOCUMENT_CHECK
        appraisal.save(user=user)
    return review
