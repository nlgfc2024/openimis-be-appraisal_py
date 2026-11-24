"""Creating and refreshing appraisals from imported applications.

An application imported or updated by msr_etl gets one appraisal per programme that appraises its source, with
a checklist item per eligibility rule pre-filled from its answers (appraisal.prefill). A repeat application,
or a document that arrives later, refreshes the system's values; an officer's answer is never changed, and an
item whose basis changed after an officer answered it, or after the eligibility decision, is flagged
``basis_changed`` for review.
"""
import logging

from django.db import transaction

from appraisal.apps import AppraisalConfig
from appraisal.models import Appraisal, AppraisalItem
from appraisal.prefill import ApplicationContext, evaluate_item, score_inputs

logger = logging.getLogger(__name__)

SCORE_INPUTS = "score_inputs"
# Statuses after the eligibility decision, when a changed system value has to be reviewed
DECIDED = {Appraisal.Status.ELIGIBLE, Appraisal.Status.INELIGIBLE, Appraisal.Status.SCORED,
           Appraisal.Status.SHORTLISTED, Appraisal.Status.NOT_SHORTLISTED}


def _is_newer(submission, other):
    """Whether ``submission`` was made after ``other``: by submission time, which holds across Kobo forms, else
    by Kobo id (comparable within one form)."""
    mine, theirs = _naive(submission.submitted_at), _naive(other.submitted_at)
    if mine and theirs and mine != theirs:
        return mine > theirs
    return submission.kobo_id > other.kobo_id


def _naive(moment):
    """A just-imported submission's time can be aware while a saved one (USE_TZ off) is naive."""
    from django.utils import timezone

    return timezone.make_naive(moment) if moment and timezone.is_aware(moment) else moment


def programmes_for(source_type):
    """{code: configuration} of the programmes that appraise applications from ``source_type``."""
    return {code: programme for code, programme in (AppraisalConfig.programmes or {}).items()
            if source_type in programme.get("source_types", [])}


def apply_submission(submission, user=None, programme=None):
    """Create or refresh the appraisals of an imported application, for every programme appraising its source
    or only ``programme``; returns them. ``user`` defaults to the one who last updated the individual (the
    import)."""
    if submission.individual_id is None:
        return []
    programmes = {code: config for code, config in programmes_for(submission.source_type).items()
                  if programme is None or code == programme}
    if not programmes:
        return []
    from individual.models import Individual

    with transaction.atomic():
        # One import at a time per person, so two applications can't both create their appraisal
        individual = Individual.objects.select_for_update().get(pk=submission.individual_id)
        user = user or individual.user_updated
        context = ApplicationContext(submission)
        return [_apply(individual, code, programme, submission, context, user)
                for code, programme in programmes.items()]


def refresh_for_document(document):
    """Refresh the appraisals of the application ``document`` belongs to, now that it is stored. An older
    application's document doesn't change an appraisal that has moved on to a newer application."""
    appraisals = Appraisal.objects.filter(submission_id=document.submission_id, is_deleted=False)
    if appraisals.exists():
        return apply_submission(document.submission)
    return []


def _apply(individual, code, programme, submission, context, user):
    appraisal = Appraisal.objects.filter(
        individual=individual, programme=code, round=programme["round"], is_deleted=False).first()
    if appraisal is None:
        appraisal = Appraisal(individual=individual, programme=code, round=programme["round"],
                              submission=submission, config_snapshot=programme)
    elif appraisal.submission_id and appraisal.submission_id != submission.id and \
            _is_newer(appraisal.submission, submission):
        logger.info("Appraisal %s already uses a newer application than Kobo %s", appraisal.id, submission.kobo_id)
        return appraisal
    else:
        appraisal.submission = submission
    # The rules in force when the appraisal was created keep applying to it
    rules = appraisal.config_snapshot or programme
    appraisal.json_ext = {**(appraisal.json_ext or {}), SCORE_INPUTS: _serialisable(score_inputs(rules, context))}
    appraisal.save(user=user)
    for item in rules.get("eligibility", []):
        _apply_item(appraisal, item, evaluate_item(item, context), user)
    return appraisal


def _apply_item(appraisal, config, outcome, user):
    basis = _serialisable(outcome.basis)
    item = AppraisalItem.objects.filter(appraisal=appraisal, kind=AppraisalItem.Kind.ELIGIBILITY,
                                        key=config["key"], is_deleted=False).first()
    if item is None:
        AppraisalItem(appraisal=appraisal, kind=AppraisalItem.Kind.ELIGIBILITY, key=config["key"],
                      system_result=outcome.result, basis=basis).save(user=user)
        return
    if (item.system_result, item.basis) == (outcome.result, basis):
        return
    if item.is_overridden or appraisal.status in DECIDED:
        item.basis_changed = True
    item.system_result, item.basis = outcome.result, basis
    item.save(user=user)


def _serialisable(values):
    """JSON-safe copy: dates and decimals as text."""
    import json

    from django.core.serializers.json import DjangoJSONEncoder

    return json.loads(json.dumps(values, cls=DjangoJSONEncoder))
