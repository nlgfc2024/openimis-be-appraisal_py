"""msr_etl signal receivers, connected in AppraisalConfig.ready(). msr_etl logs a receiver's error without
interrupting its import or download; appraisal_backfill brings a missed application up to date."""
from appraisal import services


def on_submission_applied(sender, submission, individual, status, **kwargs):
    services.apply_submission(submission)


def on_document_stored(sender, document, **kwargs):
    services.refresh_for_document(document)


def connect():
    from msr_etl.signals import applicant_document_stored, kobo_submission_applied

    kobo_submission_applied.connect(on_submission_applied, dispatch_uid="appraisal_on_submission_applied")
    applicant_document_stored.connect(on_document_stored, dispatch_uid="appraisal_on_document_stored")
