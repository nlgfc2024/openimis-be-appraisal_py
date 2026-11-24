"""Built-in checklist checks that read an application's uploaded templates. A template is filled in by the
applicant, so what it says is proposed to the officer, never decided: the result is Pending, with the
proposal (``proposed``: YES or NO) and the figures in the basis. A missing, still downloading or unreadable
template stays Pending with the reason.

Every limit, answer name, template and column comes from the item's ``params`` (``CHECK_PARAMS`` says which
are required), so each programme states its own rules. A header matches when it starts with the configured
column name, ignoring case and spacing ("Applicant contribution" matches "APPLICANT CONTRIBUTION (30%)").
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
    legal_form = context.answer(params["legal_form_field"])
    min_age, max_age = params["min_age"], params["max_age"]
    if legal_form is None:
        return Outcome(Result.PENDING, {"note": "No legal form"})
    if legal_form in params["sole_forms"]:
        born = to_date(context.answer(params["dob_field"]))
        if born is None:
            return Outcome(Result.PENDING, {"legal_form": legal_form, "note": "No date of birth"})
        age = completed_years(born, context.reference_date)
        youth = min_age <= age <= max_age
        return Outcome(Result.YES if youth else Result.NO,
                       {"legal_form": legal_form, "age": age, "note": f"Sole proprietor aged {age}"})
    try:
        owners = read_template(context, params["template"], params["columns"])
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
    minimum = params["min_share"]
    return Outcome(Result.PENDING, {
        "proposed": Result.YES if youth_share > minimum else Result.NO, "legal_form": legal_form,
        "owners": len(shares), "youth_share": youth_share, "total_share": total,
        "note": f"{youth_share}% owned by owners aged {min_age}-{max_age} (more than {minimum}% needed)",
    })


def co_financing(context, params):
    """Co-financing (Annex 2A): the applicant's contribution is at least ``min_share`` % of the investment in
    the grant request template (applicant + project contributions)."""
    sources = context.answer(params["sources_field"]) if params.get("sources_field") else None
    try:
        rows = read_template(context, params["template"], params["columns"])
    except TemplateUnavailable as exc:
        return Outcome(Result.PENDING, {"sources": sources, "note": str(exc)})
    # A filled cell that isn't a number leaves the item Pending rather than counting as 0
    cells = [row[key] for row in rows for key in ("applicant", "project")]
    unreadable = [str(cell) for cell in cells if str(cell).strip() and to_number(cell) is None]
    if unreadable:
        return Outcome(Result.PENDING, {"sources": sources,
                                        "note": f"Contributions can't be read: {', '.join(unreadable)}"})
    applicant = sum(to_number(row["applicant"]) or 0 for row in rows)
    project = sum(to_number(row["project"]) or 0 for row in rows)
    if applicant + project <= 0:
        return Outcome(Result.PENDING, {"sources": sources, "note": "No contributions in the grant request template"})
    share = round(applicant / (applicant + project) * 100, 2)
    minimum = params["min_share"]
    return Outcome(Result.PENDING, {
        "proposed": Result.YES if share >= minimum else Result.NO, "applicant": applicant, "project": project,
        "share": share, "sources": sources, "note": f"Applicant contributes {share}% (at least {minimum}% needed)",
    })


CHECKS = {
    "youth_ownership": youth_ownership,
    "co_financing": co_financing,
}

TEXT, NUMBER, TEXT_LIST = "text", "number", "list of text"
# {check: {param: kind}} of the required params; a "columns" entry lists the column keys it must name
CHECK_PARAMS = {
    "youth_ownership": {"legal_form_field": TEXT, "sole_forms": TEXT_LIST, "dob_field": TEXT, "min_age": NUMBER,
                        "max_age": NUMBER, "min_share": NUMBER, "template": TEXT, "columns": ("age", "share")},
    "co_financing": {"template": TEXT, "min_share": NUMBER, "columns": ("applicant", "project")},
}
OPTIONAL_PARAMS = {"co_financing": {"sources_field": TEXT}}


def _is_kind(value, kind):
    if kind == NUMBER:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == TEXT_LIST:
        return isinstance(value, list) and bool(value) and all(_is_kind(v, TEXT) for v in value)
    return isinstance(value, str) and bool(value.strip())


def params_errors(check, params, path):
    """Messages for ``params`` that ``check`` can't run with."""
    errors = []
    required, optional = CHECK_PARAMS[check], OPTIONAL_PARAMS.get(check, {})
    for name in sorted(set(params) - set(required) - set(optional)):
        errors.append(f"{path}.{name} is not a parameter of {check}.")
    for name, kind in {**required, **optional}.items():
        if name not in params:
            if name in required:
                errors.append(f"{path}.{name} is required by {check}.")
        elif isinstance(kind, tuple):
            columns = params[name]
            if not isinstance(columns, dict) or set(columns) != set(kind) or \
                    not all(_is_kind(v, TEXT) for v in columns.values()):
                errors.append(f"{path}.{name} must name the columns {', '.join(kind)}.")
        elif not _is_kind(params[name], kind):
            errors.append(f"{path}.{name} must be a {kind}.")
    return errors
