"""Built-in checklist checks that read an application's uploaded templates. A template is filled in by the
applicant, so what it says is proposed to the officer, never decided: the result is Pending, with the
proposal (``proposed``: YES or NO) and the figures in the basis. A missing, still downloading or unreadable
template stays Pending with the reason.

Column names default to the Urban MEP templates and can be set in the item's ``params``; a header matches
when it starts with the name, ignoring case and spacing ("Applicant contribution" matches
"APPLICANT CONTRIBUTION (30%)").
"""
import re

from calcrule_validations.strategies.parameter_helpers import completed_years, to_date, to_number

from appraisal.models import AppraisalItem
from appraisal.prefill import Outcome

Result = AppraisalItem.Result


class TemplateUnavailable(Exception):
    """The template can't be read; the message says why, for the officer."""


def _normal(text):
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def read_template(context, document_type, columns):
    """[{column key: cell text}] of the filled rows under the first header row holding every column in
    ``columns`` ({key: header name}), from the application's stored ``document_type`` upload."""
    from msr_etl.kobo_documents import TableError, read_table
    from msr_etl.models import ApplicantDocument

    document = context.documents([document_type]).order_by("-created_at").first()
    if document is None:
        raise TemplateUnavailable(f"No {document_type} uploaded")
    if document.status == ApplicantDocument.Status.PENDING:
        raise TemplateUnavailable(f"{document_type} is still being downloaded")
    if document.status != ApplicantDocument.Status.STORED:
        raise TemplateUnavailable(f"{document_type} could not be downloaded")
    try:
        sheets = read_table(document)
    except TableError as exc:
        raise TemplateUnavailable(f"{document_type}: {exc}") from exc
    wanted = {key: _normal(name) for key, name in columns.items()}
    for sheet in sheets:
        rows = sheet["rows"]
        for index, row in enumerate(rows):
            headers = [_normal(cell) for cell in row]
            found = {key: next((i for i, h in enumerate(headers) if h and h.startswith(name)), None)
                     for key, name in wanted.items()}
            if None not in found.values():
                return [{key: (row[i] if i < len(row) else "") for key, i in found.items()}
                        for row in rows[index + 1:] if any(str(cell).strip() for cell in row)]
    raise TemplateUnavailable(f"{document_type} doesn't have the columns {', '.join(columns.values())}")


def _share(text):
    """A percentage from "60", "60%" or "0.6" (a cell formatted as a percentage); None if it isn't a number."""
    text = str(text or "").strip()
    return to_number(text.rstrip("%").strip()) if text else None


def youth_ownership(context, params):
    """Youth ownership (Annex 2A): a sole proprietor must be a youth; a partnership or company must be more
    than ``min_share`` % owned by owners aged ``min_age``-``max_age``, as its ownership template says."""
    legal_form = context.answer(params.get("legal_form_field", "legal_form"))
    min_age, max_age = params.get("min_age", 18), params.get("max_age", 35)
    if legal_form is None:
        return Outcome(Result.PENDING, {"note": "No legal form"})
    if legal_form in params.get("sole_forms", ["sole_proprietorship"]):
        born = to_date(context.answer(params.get("dob_field", "dob")))
        if born is None:
            return Outcome(Result.PENDING, {"legal_form": legal_form, "note": "No date of birth"})
        age = completed_years(born, context.reference_date)
        youth = min_age <= age <= max_age
        return Outcome(Result.YES if youth else Result.NO,
                       {"legal_form": legal_form, "age": age, "note": f"Sole proprietor aged {age}"})
    columns = {"age": "Age", "share": "Ownership %", **params.get("columns", {})}
    try:
        owners = read_template(context, params.get("template", "ownership_template"), columns)
    except TemplateUnavailable as exc:
        return Outcome(Result.PENDING, {"legal_form": legal_form, "note": str(exc)})
    # Rows with neither an age nor a share are unused template lines (e.g. only their "No." filled)
    shares = [(to_number(owner["age"]), _share(owner["share"])) for owner in owners
              if str(owner["age"]).strip() or str(owner["share"]).strip()]
    if not shares or any(age is None or share is None for age, share in shares):
        return Outcome(Result.PENDING, {"legal_form": legal_form, "note": "Owners' ages or shares can't be read"})
    if sum(share for _, share in shares) <= 1.0001:
        shares = [(age, share * 100) for age, share in shares]
    total = round(sum(share for _, share in shares), 2)
    youth_share = round(sum(share for age, share in shares if min_age <= age <= max_age), 2)
    minimum = params.get("min_share", 80)
    return Outcome(Result.PENDING, {
        "proposed": Result.YES if youth_share > minimum else Result.NO, "legal_form": legal_form,
        "owners": len(shares), "youth_share": youth_share, "total_share": total,
        "note": f"{youth_share}% owned by owners aged {min_age}-{max_age} (more than {minimum}% needed)",
    })


def co_financing(context, params):
    """Co-financing (Annex 2A): the applicant's contribution is at least ``min_share`` % of the investment in
    the grant request template (applicant + project contributions)."""
    columns = {"applicant": "Applicant contribution", "project": "Project contribution", **params.get("columns", {})}
    sources = context.answer(params.get("sources_field", "contribution_sources"))
    try:
        rows = read_template(context, params.get("template", "grant_request_template"), columns)
    except TemplateUnavailable as exc:
        return Outcome(Result.PENDING, {"sources": sources, "note": str(exc)})
    applicant = sum(to_number(row["applicant"]) or 0 for row in rows)
    project = sum(to_number(row["project"]) or 0 for row in rows)
    if applicant + project <= 0:
        return Outcome(Result.PENDING, {"sources": sources, "note": "No contributions in the grant request template"})
    share = round(applicant / (applicant + project) * 100, 2)
    minimum = params.get("min_share", 30)
    return Outcome(Result.PENDING, {
        "proposed": Result.YES if share >= minimum else Result.NO, "applicant": applicant, "project": project,
        "share": share, "sources": sources, "note": f"Applicant contributes {share}% (at least {minimum}% needed)",
    })


CHECKS = {
    "youth_ownership": youth_ownership,
    "co_financing": co_financing,
}
