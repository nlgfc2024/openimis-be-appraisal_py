"""Pre-filling an appraisal from its application: each checklist item's proposed result, and the inputs its score
components read. Nothing here writes; services.apply_submission stores the outcomes.

- A checklist item with a ``strategy`` is a calcrule_validations strategy run on one answer; with a ``check``, one
  of the built-in checks in appraisal.checks; with neither, it is left Pending for the officer.
- A missing answer is always Pending, never No: the officer asks for it or decides.
- Score inputs are answers, or values derived from answers and documents as the programme's ``inputs`` say.
"""
from dataclasses import dataclass, field

from calcrule_validations.strategies import ValidationStrategyStorage
from calcrule_validations.strategies.parameter_helpers import is_blank, to_date

from appraisal.models import AppraisalItem

Result = AppraisalItem.Result


@dataclass(frozen=True)
class Outcome:
    result: str
    basis: dict = field(default_factory=dict)


class ApplicationContext:
    """What the pre-fill reads of one application: its answers, its documents and the date it was submitted,
    which ages, expiry dates and periods are worked out on."""

    def __init__(self, submission):
        from msr_etl import kobo_review

        self.submission = submission
        self.answers = kobo_review.answers(submission)
        submitted = submission.submitted_at or to_date((submission.data or {}).get("_submission_time")) \
            or submission.created_at
        self.reference_date = to_date(submitted)

    def answer(self, name, location_type=None):
        """The answer to question ``name``, None when unanswered. A location code is followed to its current
        code, so an answer given with a since-replaced temporary code still matches."""
        value = self.answers.get(name)
        if is_blank(value):
            return None
        if location_type:
            from msr_etl.location_file import current_location_code

            return current_location_code(location_type, str(value).strip())
        return value

    def documents(self, document_types):
        from msr_etl.models import ApplicantDocument

        return ApplicantDocument.objects.filter(submission=self.submission, document_type__in=list(document_types))


def evaluate_item(item, context):
    """The proposed Outcome of checklist item ``item`` (its configuration) for this application."""
    if item.get("check"):
        from appraisal.checks import CHECKS

        return CHECKS[item["check"]](context, item.get("params") or {})
    if not item.get("strategy"):
        return Outcome(Result.PENDING, {"note": "Decided by an officer"})
    name = item["field"]
    value = context.answer(name, item.get("location_type"))
    if value is None:
        return Outcome(Result.PENDING, {"field": name, "answer": None, "note": "No answer"})
    strategy = ValidationStrategyStorage.choose_strategy(item["strategy"])
    checked = strategy.validate(name, value, reference_date=context.reference_date, **(item.get("params") or {}))
    return Outcome(Result.YES if checked["success"] else Result.NO,
                   {"field": name, "answer": value, "strategy": item["strategy"], "note": checked["note"]})


def months_between(start, end):
    """Whole months from ``start`` to ``end``; None if either is missing or start is after end."""
    if start is None or end is None or start > end:
        return None
    return (end.year - start.year) * 12 + end.month - start.month - (end.day < start.day)


def _months_since(context, spec):
    return months_between(to_date(context.answer(spec["field"])), context.reference_date)


def _all_answered(context, spec):
    return all(context.answer(name) is not None for name in spec["fields"])


def _document_stored(context, spec):
    """True when one of the documents is stored, False when there are none or all failed, None while one is
    still downloading (the value comes once it's stored)."""
    from msr_etl.models import ApplicantDocument

    statuses = set(context.documents(spec["document_types"]).values_list("status", flat=True))
    if ApplicantDocument.Status.STORED in statuses:
        return True
    return None if ApplicantDocument.Status.PENDING in statuses else False


# How a programme's ``inputs`` derive a score input: {"derive": <name>, ...the keys each one needs}
DERIVATIONS = {
    "months_since": (_months_since, ("field",)),
    "all_answered": (_all_answered, ("fields",)),
    "document_stored": (_document_stored, ("document_types",)),
}


def score_inputs(programme, context):
    """{field: value} of every score component's field: a derived input when the programme's ``inputs`` define
    it, else the answer. Unknown values are None, which scores nothing until an officer decides."""
    derived = programme.get("inputs") or {}
    values = {}
    for component in (programme.get("scoring") or {}).get("components", []):
        name = component["field"]
        spec = derived.get(name)
        values[name] = DERIVATIONS[spec["derive"]][0](context, spec) if spec else context.answer(name)
    return values
