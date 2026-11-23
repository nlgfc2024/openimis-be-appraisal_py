import io
import shutil
import tempfile
from datetime import date, datetime, timezone
from io import StringIO
from unittest.mock import patch

import openpyxl
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone as django_timezone

from appraisal.apps import AppraisalConfig
from appraisal.config import validate_programmes
from appraisal.models import Appraisal, AppraisalItem
from appraisal.prefill import months_between
from appraisal.services import apply_submission
from core.test_helpers import create_admin_role, create_test_interactive_user
from individual.tests.test_helpers import create_individual
from location.models import Location
from msr_etl import signals
from msr_etl.models import ApplicantDocument, KoboSubmission, MsrEtlLocationCodeChange

Result = AppraisalItem.Result
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SUBMITTED = datetime(2026, 11, 23, 8, 30, tzinfo=timezone.utc)

PROGRAMME = {
    "source_types": ["kobo"],
    "round": "2026-R1",
    "eligibility": [
        {"key": "youth_age", "label": "Youth aged 18-35", "field": "dob",
         "strategy": "AgeRangeValidationStrategy", "params": {"min": 18, "max": 35}},
        {"key": "education", "label": "JCE or higher", "field": "highest_education",
         "strategy": "ValueInListValidationStrategy", "params": {"values": ["jce", "msce"]}},
        {"key": "registration", "label": "Valid registration", "field": "business_reg_expiry_date",
         "strategy": "DateAfterValidationStrategy"},
        {"key": "designated_city", "label": "Designated city", "field": "biz_city", "location_type": "R",
         "strategy": "ValueInListValidationStrategy", "params": {"values": ["325", "LL01"]}},
        {"key": "legally_established", "label": "Legally established", "strategy": None},
        {"key": "youth_ownership", "label": "Youth ownership", "check": "youth_ownership"},
        {"key": "co_financing", "label": "Co-financing", "check": "co_financing"},
    ],
    "inputs": {
        "months_in_operation": {"derive": "months_since", "field": "business_start_date"},
        "has_business_bank_account": {"derive": "all_answered",
                                      "fields": ["business_account_name", "business_account_number"]},
        "has_bank_statement": {"derive": "document_stored", "document_types": ["bank_statement_3m"]},
    },
    "scoring": {"normalise_to": 100, "components": [
        {"key": "duration", "label": "Duration", "field": "months_in_operation", "weight": 15,
         "scoring": {"type": "numeric_bands", "bands": [{"min": 12, "points": 15}]}},
        {"key": "account", "label": "Account", "field": "has_business_bank_account", "weight": 10,
         "scoring": {"type": "mapping", "values": {"true": 10}}},
        {"key": "statement", "label": "Statement", "field": "has_bank_statement", "weight": 10,
         "scoring": {"type": "mapping", "values": {"true": 10}}},
        {"key": "jobs", "label": "Jobs", "field": "jobs_to_create", "weight": 25,
         "scoring": {"type": "numeric_bands", "bands": [{"min": 1, "points": 25}]}},
    ]},
}

ANSWERS = {
    "applicant/dob": "1995-04-02", "applicant/highest_education": "msce",
    "enterprise/business_reg_expiry_date": "2027-06-30", "enterprise/legal_form": "sole_proprietorship",
    "enterprise/business_start_date": "2024-11-24", "business_location/biz_city": "325",
    "performance/business_account_name": "Banda Tailoring", "performance/business_account_number": "1002003004",
    "employment/jobs_to_create": "4", "investment/contribution_sources": "business_savings",
}


def workbook(rows):
    """An uploaded template: a blank first row and the header in row 2, as in the Urban MEP templates."""
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append([])
    for row in rows:
        sheet.append(row)
    content = io.BytesIO()
    book.save(content)
    return content.getvalue()


OWNERS = ["No.", "Owner Name", "Sex", "Age", "Ownership %", "National ID Number"]
GRANT = ["REQUIRED INVESTMENT", "COST", "APPLICANT CONTRIBUTION (30%)", "PROJECT CONTRIBUTION (70%)", "TOTAL"]


class PrefillTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_interactive_user(username="appraisalImporter", roles=[create_admin_role().id])

    def setUp(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        settings = override_settings(MEDIA_ROOT=folder)
        settings.enable()
        self.addCleanup(settings.disable)
        patcher = patch.object(AppraisalConfig, "programmes", {"UMEP": PROGRAMME})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.individual = create_individual(self.user.username)

    def _submission(self, kobo_id=1, **answers):
        return KoboSubmission.objects.create(
            source_type="kobo", asset_uid="aForm", kobo_id=kobo_id, submitted_at=SUBMITTED,
            data={"_id": kobo_id, "_submission_time": "2026-11-23T08:30:00", **ANSWERS, **answers},
            individual=self.individual, status=KoboSubmission.Status.IMPORTED)

    def _document(self, submission, document_type, content=None, status="STORED"):
        path = default_storage.save(f"kobo_documents/test/{document_type}.xlsx", ContentFile(content or b"")) \
            if content is not None else ""
        return ApplicantDocument.objects.create(
            submission=submission, individual=self.individual, kobo_attachment_uid=f"att{document_type}",
            question=f"x/{document_type}", document_type=document_type, filename=f"{document_type}.xlsx",
            mimetype=XLSX, size=len(content or b""), storage_path=path, status=status)

    def _items(self, appraisal):
        return {item.key: item for item in appraisal.items.filter(is_deleted=False)}

    def _apply(self, submission):
        [appraisal] = apply_submission(submission)
        return appraisal, self._items(appraisal)


class ApplySubmissionTest(PrefillTestCase):
    def test_an_application_gets_its_appraisal_and_checklist(self):
        appraisal, items = self._apply(self._submission(highest_education="pslc"))

        self.assertEqual((appraisal.programme, appraisal.round, appraisal.submission.kobo_id), ("UMEP", "2026-R1", 1))
        self.assertEqual(appraisal.config_snapshot, PROGRAMME)
        self.assertEqual(appraisal.user_created, self.individual.user_updated)
        results = {key: item.system_result for key, item in items.items()}
        self.assertEqual(results, {"youth_age": "YES", "education": "NO", "registration": "YES",
                                   "designated_city": "YES", "legally_established": "PENDING",
                                   "youth_ownership": "YES", "co_financing": "PENDING"})
        self.assertEqual(items["youth_age"].basis["note"], "Aged 31 on 2026-11-23")
        self.assertEqual(items["education"].basis, {"field": "highest_education", "answer": "pslc",
                                                    "strategy": "ValueInListValidationStrategy",
                                                    "note": "'pslc' is not one of: jce, msce"})
        self.assertEqual(items["legally_established"].basis, {"note": "Decided by an officer"})

    def test_a_missing_answer_is_pending_not_no(self):
        _, items = self._apply(self._submission(**{"applicant/highest_education": None}))

        self.assertEqual((items["education"].system_result, items["education"].basis["note"]), ("PENDING", "No answer"))

    def test_expiry_and_age_are_checked_on_the_submission_date(self):
        _, items = self._apply(self._submission(**{"applicant/dob": "1990-11-23",
                                                   "enterprise/business_reg_expiry_date": "2026-11-23"}))

        self.assertEqual(items["youth_age"].system_result, "NO")
        self.assertEqual(items["registration"].system_result, "YES")

    def test_a_replaced_temporary_city_code_still_matches(self):
        city = Location.objects.create(code="LL01", name="Lilongwe", type="R", audit_user_id=-1)
        MsrEtlLocationCodeChange.objects.create(location_id=city.id, location_type="R", old_code="LL", new_code="LL01")

        _, items = self._apply(self._submission(**{"business_location/biz_city": "LL"}))

        self.assertEqual((items["designated_city"].system_result, items["designated_city"].basis["answer"]),
                         ("YES", "LL01"))

    def test_score_inputs_are_derived_and_stored(self):
        submission = self._submission()
        self._document(submission, "bank_statement_3m", b"%PDF", status="PENDING")

        appraisal, _ = self._apply(submission)

        self.assertEqual(appraisal.json_ext["score_inputs"], {
            "months_in_operation": 23, "has_business_bank_account": True, "has_bank_statement": None,
            "jobs_to_create": "4"})

    def test_programmes_not_reading_the_source_are_left_alone(self):
        submission = self._submission()
        submission.source_type = "ubr"
        self.assertEqual(apply_submission(submission), [])
        self.assertFalse(Appraisal.objects.exists())


class RefreshTest(PrefillTestCase):
    def test_an_officer_answer_is_kept_and_a_changed_basis_flagged(self):
        appraisal, items = self._apply(self._submission(highest_education="pslc"))
        education = items["education"]
        education.officer_result, education.decided_by, education.decided_at = "YES", self.user, django_timezone.now()
        education.save(user=self.user)

        _, items = self._apply(self._submission(2, **{"applicant/highest_education": "jce"}))

        education = items["education"]
        self.assertEqual((education.system_result, education.officer_result, education.result), ("YES", "YES", "YES"))
        self.assertTrue(education.basis_changed)
        self.assertFalse(items["youth_age"].basis_changed)

    def test_an_unchanged_refresh_writes_nothing(self):
        submission = self._submission()
        appraisal, items = self._apply(submission)

        self._apply(submission)

        self.assertEqual(Appraisal.objects.get(id=appraisal.id).version, appraisal.version)
        self.assertEqual(AppraisalItem.objects.get(id=items["youth_age"].id).version, 1)

    def test_a_newer_application_replaces_the_older_one_but_not_the_reverse(self):
        older, newer = self._submission(1), self._submission(2, **{"applicant/highest_education": "pslc"})
        self._apply(newer)

        appraisal, items = self._apply(older)

        self.assertEqual(appraisal.submission_id, newer.id)
        self.assertEqual(items["education"].system_result, "NO")
        self.assertEqual(Appraisal.objects.count(), 1)

    def test_the_rules_in_force_at_creation_keep_applying(self):
        submission = self._submission()
        self._apply(submission)
        changed = {**PROGRAMME, "eligibility": PROGRAMME["eligibility"][:1]}

        with patch.object(AppraisalConfig, "programmes", {"UMEP": changed}):
            _, items = self._apply(submission)

        self.assertEqual(len(items), len(PROGRAMME["eligibility"]))


class TemplateChecksTest(PrefillTestCase):
    def _partnership(self, owners=None, kobo_id=1, **answers):
        submission = self._submission(kobo_id, **{"enterprise/legal_form": "partnership_group", **answers})
        if owners is not None:
            self._document(submission, "ownership_template", workbook([OWNERS, *owners]))
        return submission

    def test_a_sole_proprietor_is_youth_owned_when_aged_18_to_35(self):
        _, items = self._apply(self._submission())
        self.assertEqual(items["youth_ownership"].basis["note"], "Sole proprietor aged 31")

        _, items = self._apply(self._submission(2, **{"applicant/dob": "1980-01-01"}))
        self.assertEqual(items["youth_ownership"].system_result, "NO")

    def test_partnership_ownership_is_proposed_from_the_template(self):
        owners = [[1, "Grace", "F", 30, 60, "AB12CD34"], [2, "John", "M", 34, 25, "XY98ZW76"],
                  [3, "Mary", "F", 52, 15, "QW12ER34"], [4, None, None, None, None, None]]
        _, items = self._apply(self._partnership(owners))

        basis = items["youth_ownership"].basis
        self.assertEqual(items["youth_ownership"].system_result, "PENDING")
        self.assertEqual((basis["proposed"], basis["youth_share"], basis["total_share"], basis["owners"]),
                         ("YES", 85.0, 100.0, 3))

    def test_exactly_80_percent_is_not_more_than_80(self):
        _, items = self._apply(self._partnership([[1, "A", "F", 30, 80], [2, "B", "M", 40, 20]]))
        self.assertEqual(items["youth_ownership"].basis["proposed"], "NO")

    def test_shares_in_a_percentage_formatted_cell_are_read_as_fractions(self):
        _, items = self._apply(self._partnership([[1, "A", "F", 30, 0.9], [2, "B", "M", 40, 0.1]]))
        self.assertEqual(items["youth_ownership"].basis["youth_share"], 90.0)

    def test_an_unusable_ownership_template_stays_pending_with_the_reason(self):
        cases = [
            (None, "No ownership_template uploaded"),
            ([[1, "A", "F", "thirty", 60]], "Owners' ages or shares can't be read"),
        ]
        for kobo_id, (owners, note) in enumerate(cases, start=1):
            with self.subTest(note=note):
                _, items = self._apply(self._partnership(owners, kobo_id=kobo_id))
                self.assertEqual((items["youth_ownership"].system_result, items["youth_ownership"].basis["note"]),
                                 ("PENDING", note))

    def test_a_template_without_the_columns_is_reported(self):
        submission = self._partnership()
        self._document(submission, "ownership_template", workbook([["Name", "Share"], ["A", 100]]))
        _, items = self._apply(submission)
        self.assertEqual(items["youth_ownership"].basis["note"],
                         "ownership_template doesn't have the columns Age, Ownership %")

    def test_co_financing_is_proposed_from_the_grant_request(self):
        submission = self._submission()
        self._document(submission, "grant_request_template", workbook([
            GRANT, ["Sewing machines", 2000000, 600000, 1400000, None], ["Fabric", 500000, 150000, 350000, None],
            ["Total Investment Cost", 2500000, None, None, None]]))

        _, items = self._apply(submission)

        basis = items["co_financing"].basis
        self.assertEqual((items["co_financing"].system_result, basis["proposed"], basis["share"], basis["sources"]),
                         ("PENDING", "YES", 30.0, "business_savings"))

    def test_co_financing_below_the_minimum_is_proposed_no(self):
        submission = self._submission()
        self._document(submission, "grant_request_template", workbook([GRANT, ["Oven", 1000000, 200000, 800000]]))
        _, items = self._apply(submission)
        self.assertEqual((items["co_financing"].basis["proposed"], items["co_financing"].basis["share"]), ("NO", 20.0))


class SignalTest(PrefillTestCase):
    def test_an_applied_application_is_appraised(self):
        submission = self._submission()

        signals.send(signals.kobo_submission_applied, KoboSubmission, submission=submission,
                     individual=self.individual, status="IMPORTED")

        self.assertEqual(Appraisal.objects.get().submission_id, submission.id)

    def test_a_template_stored_later_refreshes_its_check(self):
        submission = self._submission(**{"enterprise/legal_form": "limited_company"})
        document = self._document(submission, "ownership_template", None, status="PENDING")
        _, items = self._apply(submission)
        self.assertEqual(items["youth_ownership"].basis["note"], "ownership_template is still being downloaded")

        path = default_storage.save("kobo_documents/test/owners.xlsx",
                                    ContentFile(workbook([OWNERS, [1, "A", "F", 25, 100]])))
        ApplicantDocument.objects.filter(id=document.id).update(status="STORED", storage_path=path, size=1)
        signals.send(signals.applicant_document_stored, ApplicantDocument,
                     document=ApplicantDocument.objects.get(id=document.id))

        item = AppraisalItem.objects.get(key="youth_ownership", is_deleted=False)
        self.assertEqual((item.basis["proposed"], item.basis["youth_share"]), ("YES", 100.0))

    def test_a_document_of_a_superseded_application_changes_nothing(self):
        older = self._submission(1)
        self._apply(self._submission(2))
        document = self._document(older, "ownership_template", workbook([OWNERS]))

        signals.send(signals.applicant_document_stored, ApplicantDocument, document=document)

        self.assertEqual(Appraisal.objects.get().submission.kobo_id, 2)


class BackfillTest(PrefillTestCase):
    def test_each_person_is_appraised_from_their_latest_application(self):
        self._submission(1)
        self._submission(3, **{"applicant/highest_education": "pslc"})
        KoboSubmission.objects.create(source_type="kobo", asset_uid="aForm", kobo_id=4, data={}, status="HELD")
        out = StringIO()

        call_command("appraisal_backfill", stdout=out)

        appraisal = Appraisal.objects.get()
        self.assertEqual(appraisal.submission.kobo_id, 3)
        self.assertIn("1 applications, 1 appraisals created or refreshed", out.getvalue())

    def test_dry_run_and_unknown_programme(self):
        self._submission()
        out = StringIO()
        call_command("appraisal_backfill", "--dry-run", stdout=out)
        self.assertIn("1 applications would be appraised", out.getvalue())
        self.assertFalse(Appraisal.objects.exists())
        with self.assertRaisesMessage(CommandError, "Programme PWP is not configured; configured: UMEP"):
            call_command("appraisal_backfill", "--programme", "PWP")


class DerivationTest(SimpleTestCase):
    def test_months_between(self):
        cases = [((date(2024, 11, 24), date(2026, 11, 23)), 23), ((date(2024, 11, 23), date(2026, 11, 23)), 24),
                 ((date(2026, 1, 31), date(2026, 2, 28)), 0), ((date(2027, 1, 1), date(2026, 1, 1)), None),
                 ((None, date(2026, 1, 1)), None)]
        for (start, end), months in cases:
            with self.subTest(start=start, end=end):
                self.assertEqual(months_between(start, end), months)


class PrefillConfigTest(SimpleTestCase):
    def assertRejected(self, item=None, inputs=None, message=""):
        programme = {"source_types": ["kobo"], "round": "R1"}
        if item:
            programme["eligibility"] = [{"key": "rule", "label": "Rule", **item}]
        if inputs:
            programme["inputs"] = inputs
        with self.assertRaises(ValidationError) as exc:
            validate_programmes({"UMEP": programme})
        self.assertIn(message, exc.exception.messages)

    def test_the_test_programme_is_valid(self):
        validate_programmes({"UMEP": PROGRAMME})

    def test_names_must_exist(self):
        self.assertRejected({"field": "dob", "strategy": "AgeValidation"},
                            message="programmes.UMEP.eligibility[0].strategy 'AgeValidation' is not a "
                                    "calcrule_validations strategy.")
        self.assertRejected({"check": "ownership"},
                            message="programmes.UMEP.eligibility[0].check must be one of: co_financing, "
                                    "youth_ownership.")

    def test_one_of_strategy_or_check(self):
        self.assertRejected({"field": "dob", "strategy": "AgeRangeValidationStrategy", "check": "co_financing"},
                            message="programmes.UMEP.eligibility[0] has both a strategy and a check; use one.")

    def test_location_type(self):
        self.assertRejected({"field": "biz_city", "strategy": "ValueInListValidationStrategy", "location_type": "X"},
                            message="programmes.UMEP.eligibility[0].location_type must be one of: R, D, W, V.")

    def test_inputs(self):
        self.assertRejected(inputs={"months": {"derive": "age"}},
                            message="programmes.UMEP.inputs.months.derive must be one of: all_answered, "
                                    "document_stored, months_since.")
        self.assertRejected(inputs={"account": {"derive": "all_answered", "fields": []}},
                            message="programmes.UMEP.inputs.account.fields must be a non-empty list of names.")
        self.assertRejected(inputs={"months": {"derive": "months_since", "field": "start", "extra": 1}},
                            message="programmes.UMEP.inputs.months has unsupported keys: extra.")
