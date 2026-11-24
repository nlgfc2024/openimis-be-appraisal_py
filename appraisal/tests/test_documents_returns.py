import json
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from appraisal import documents, returns, review
from appraisal.apps import AppraisalConfig
from appraisal.config import validate_programmes
from appraisal.models import Appraisal, AppraisalItem, AppraisalReturn, DocumentReview
from appraisal.services import apply_submission
from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_admin_role, create_test_interactive_user
from individual.tests.test_helpers import create_individual
from msr_etl.models import ApplicantDocument, KoboSubmission

REQUIRED = [
    "doc_national_id",
    "doc_business_registration",
    {"type": "ownership_template", "when": {"field": "legal_form", "in": ["partnership_group", "limited_company"]}},
    {"type": "bank_statement_3m", "when": {"field": "evidence_documents", "in": ["bank_statements"]}},
]
RULES = {"source_types": ["kobo"], "round": "2026-R1",
         "eligibility": [{"key": "youth_age", "label": "Youth aged 18-35"}], "documents": {"required": REQUIRED}}
ANSWERS = {"enterprise/legal_form": "sole_proprietorship", "performance/evidence_documents": "sales_records bank_statements"}
Status = DocumentReview.Status


class DocumentsTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_interactive_user(username="appraisalDocs", roles=[create_admin_role().id])

    def setUp(self):
        patcher = patch.object(AppraisalConfig, "programmes", {"UMEP": RULES})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.individual = create_individual(self.user.username)
        self.submission = self._submission(1)
        self.national_id = self._document(self.submission, "doc_national_id")
        self.registration = self._document(self.submission, "doc_business_registration")
        self.appraisal = Appraisal(individual=self.individual, programme="UMEP", round="2026-R1",
                                   submission=self.submission, config_snapshot=RULES)
        self.appraisal.save(user=self.user)
        AppraisalItem(appraisal=self.appraisal, kind="ELIGIBILITY", key="youth_age", system_result="YES").save(
            user=self.user)

    def _submission(self, kobo_id, **answers):
        return KoboSubmission.objects.create(source_type="kobo", asset_uid="aForm", kobo_id=kobo_id,
                                             data={"_id": kobo_id, **ANSWERS, **answers}, individual=self.individual,
                                             status="IMPORTED")

    def _document(self, submission, kind):
        return ApplicantDocument.objects.create(submission=submission, individual=self.individual,
                                                kobo_attachment_uid=f"att{kind}{submission.kobo_id}",
                                                question=f"x/{kind}", document_type=kind, filename=f"{kind}.pdf",
                                                status="STORED")

    def _check(self):
        return {entry["document_type"]: (entry["status"], entry["required"])
                for entry in documents.document_check(Appraisal.objects.get(id=self.appraisal.id))}


class DocumentCheckTest(DocumentsTestCase):
    def test_uploaded_documents_are_submitted_and_missing_required_ones_outstanding(self):
        self.assertEqual(self._check(), {"doc_national_id": ("SUBMITTED", True),
                                         "doc_business_registration": ("SUBMITTED", True),
                                         "bank_statement_3m": ("OUTSTANDING", True)})

    def test_conditional_requirements_follow_the_answers(self):
        submission = self._submission(2, **{"enterprise/legal_form": "limited_company",
                                            "performance/evidence_documents": "sales_records"})
        Appraisal.objects.filter(id=self.appraisal.id).update(submission=submission)
        self.assertEqual(self._check(), {"ownership_template": ("OUTSTANDING", True),
                                         "doc_national_id": ("OUTSTANDING", True),
                                         "doc_business_registration": ("OUTSTANDING", True)})

    def test_an_officer_verifies_or_rejects_with_a_reason(self):
        review_ = documents.review_document(self.user, self.appraisal.id, self.national_id.id, "VERIFIED")
        self.assertEqual((review_.status, review_.reviewed_by_id), ("VERIFIED", self.user.id))
        appraisal = Appraisal.objects.get(id=self.appraisal.id)
        self.assertEqual((appraisal.status, appraisal.stage), ("DOCUMENT_CHECK", "DOCUMENT_CHECK"))
        with self.assertRaisesMessage(ValidationError, "appraisal.rejection_reason_required"):
            documents.review_document(self.user, self.appraisal.id, self.registration.id, "REJECTED", " ")
        documents.review_document(self.user, self.appraisal.id, self.national_id.id, "REJECTED", "Unreadable scan")
        self.assertEqual(self._check()["doc_national_id"], ("REJECTED", True))
        self.assertEqual(DocumentReview.objects.filter(document=self.national_id).count(), 1)

    def test_only_documents_of_the_appraised_application(self):
        other = self._document(self._submission(3), "doc_other")
        for document_id, status, message in ((other.id, "VERIFIED", "appraisal.document_not_found"),
                                              (self.national_id.id, "SUBMITTED", "appraisal.invalid_document_status")):
            with self.subTest(message=message), self.assertRaisesMessage(ValidationError, message):
                documents.review_document(self.user, self.appraisal.id, document_id, status)

    def test_a_document_without_its_file_can_be_rejected_but_not_verified(self):
        for status in ("PENDING", "FAILED"):
            ApplicantDocument.objects.filter(id=self.registration.id).update(status=status)
            with self.subTest(status=status), self.assertRaisesMessage(ValidationError,
                                                                       "appraisal.document_not_stored"):
                documents.review_document(self.user, self.appraisal.id, self.registration.id, "VERIFIED")
        review_ = documents.review_document(self.user, self.appraisal.id, self.registration.id, "REJECTED",
                                            "The upload failed; ask for it again")
        self.assertEqual(review_.status, "REJECTED")

    def test_only_a_decision_is_stored(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            DocumentReview(appraisal=self.appraisal, document=self.national_id, status="SUBMITTED",
                           reviewed_by=self.user, reviewed_at="2026-11-24T10:00:00").save(user=self.user)


class DocumentGatingTest(DocumentsTestCase):
    def test_outstanding_or_unverified_required_documents_block_the_decision(self):
        with self.assertRaisesMessage(ValidationError, "appraisal.documents_incomplete: doc_national_id (submitted), "
                                                       "doc_business_registration (submitted), bank_statement_3m "
                                                       "(outstanding)"):
            review.decide_eligibility(self.user, self.appraisal.id)

    def test_a_rejected_document_blocks_the_decision_even_when_not_required(self):
        statement = self._document(self.submission, "bank_statement_3m")
        other = self._document(self.submission, "doc_other")
        for document in (self.national_id, self.registration, statement):
            documents.review_document(self.user, self.appraisal.id, document.id, "VERIFIED")
        documents.review_document(self.user, self.appraisal.id, other.id, "REJECTED", "Not a business document")

        with self.assertRaisesMessage(ValidationError, "appraisal.documents_incomplete: doc_other (rejected)"):
            review.decide_eligibility(self.user, self.appraisal.id)

        documents.review_document(self.user, self.appraisal.id, other.id, "VERIFIED")
        appraisal = review.decide_eligibility(self.user, self.appraisal.id)
        self.assertEqual(appraisal.eligibility, "ELIGIBLE")

    def test_a_programme_without_documents_has_no_check(self):
        Appraisal.objects.filter(id=self.appraisal.id).update(
            config_snapshot={key: value for key, value in RULES.items() if key != "documents"})
        self.assertEqual(review.decide_eligibility(self.user, self.appraisal.id).eligibility, "ELIGIBLE")


class ReturnTest(DocumentsTestCase):
    def test_a_return_keeps_the_stage_and_blocks_work_until_resubmitted(self):
        Appraisal.objects.filter(id=self.appraisal.id).update(status="DESK_SCREENING", stage="DESK_SCREENING")
        returned = returns.return_appraisal(self.user, self.appraisal.id, "Bank statement missing",
                                            "Upload the last 3 months' statement")

        appraisal = Appraisal.objects.get(id=self.appraisal.id)
        self.assertEqual((appraisal.status, appraisal.stage), ("RETURNED", "DESK_SCREENING"))
        self.assertEqual((returned.stage, returned.previous_status, returned.is_open), ("DESK_SCREENING",
                                                                                        "DESK_SCREENING", True))
        item = appraisal.items.get()
        with self.assertRaisesMessage(ValidationError, "appraisal.screening_closed"):
            review.answer_item(self.user, item.id, "YES", "ID card")
        with self.assertRaisesMessage(ValidationError, "appraisal.not_returnable"):
            returns.return_appraisal(self.user, self.appraisal.id, "Again", "Again")

        closed = returns.resubmit_appraisal(self.user, self.appraisal.id, "Statement received by email")

        appraisal.refresh_from_db()
        self.assertEqual(appraisal.status, "DESK_SCREENING")
        self.assertEqual((closed.is_open, closed.resubmitted_by_id, closed.resubmission_note),
                         (False, self.user.id, "Statement received by email"))
        with self.assertRaisesMessage(ValidationError, "appraisal.not_returned"):
            returns.resubmit_appraisal(self.user, self.appraisal.id)

    def test_a_return_needs_its_reason_and_action_and_a_returnable_status(self):
        with self.assertRaisesMessage(ValidationError, "appraisal.return_details_required"):
            returns.return_appraisal(self.user, self.appraisal.id, "Missing", " ")
        Appraisal.objects.filter(id=self.appraisal.id).update(status="SHORTLISTED")
        with self.assertRaisesMessage(ValidationError, "appraisal.not_returnable"):
            returns.return_appraisal(self.user, self.appraisal.id, "Missing", "Upload it")

    def test_one_open_return_at_a_time(self):
        returns.return_appraisal(self.user, self.appraisal.id, "Missing", "Upload it")
        with self.assertRaises(IntegrityError), transaction.atomic():
            AppraisalReturn(appraisal=self.appraisal, stage="DOCUMENT_CHECK", previous_status="RECEIVED",
                            reason="x", action_required="y", requested_by=self.user,
                            returned_at="2026-11-24T10:00:00").save(user=self.user)

    def test_a_new_application_resubmits_a_returned_appraisal(self):
        returns.return_appraisal(self.user, self.appraisal.id, "Statement missing", "Apply again with it")
        newer = self._submission(5)

        apply_submission(newer, user=self.user)

        appraisal = Appraisal.objects.get(id=self.appraisal.id)
        self.assertEqual((appraisal.status, appraisal.submission_id), ("RECEIVED", newer.id))
        closed = appraisal.returns.get()
        self.assertEqual(closed.resubmission_note, "New application from Kobo (submission 5)")


class DocumentsReturnsConfigTest(TestCase):
    def test_document_requirements_are_checked(self):
        validate_programmes({"UMEP": RULES})
        cases = [({"required": "doc_national_id"}, "programmes.UMEP.documents.required must be a list of document types."),
                 ({"required": [{"when": {}}]},
                  "programmes.UMEP.documents.required[0] must be a document type, or an object with a type and a when."),
                 ({"required": [{"type": "x", "when": {"field": "y", "in": []}}]},
                  "programmes.UMEP.documents.required[0].when must be an object with a field and a non-empty in list.")]
        for documents_rule, message in cases:
            with self.subTest(message=message), self.assertRaises(ValidationError) as exc:
                validate_programmes({"UMEP": {**RULES, "documents": documents_rule}})
            self.assertIn(message, exc.exception.messages)


class DocumentsReturnsApiTest(openIMISGraphQLTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = create_test_interactive_user(username="appraisalDocsApi", roles=[create_admin_role().id])
        cls.token = BaseTestContext(user=cls.admin).get_jwt()

    def setUp(self):
        super().setUp()
        # Whatever the database's saved configuration, these tests run as configured
        configured = patch.object(AppraisalConfig, "config_error", None)
        configured.start()
        self.addCleanup(configured.stop)
        individual = create_individual(self.admin.username)
        submission = KoboSubmission.objects.create(source_type="kobo", asset_uid="aForm", kobo_id=77, data=ANSWERS,
                                                   individual=individual, status="IMPORTED")
        self.document = ApplicantDocument.objects.create(
            submission=submission, individual=individual, kobo_attachment_uid="attApi", question="x/doc_national_id",
            document_type="doc_national_id", filename="id.png", status="STORED")
        self.appraisal = Appraisal(individual=individual, programme="UMEP", round="2026-R1", submission=submission,
                                   config_snapshot=RULES)
        self.appraisal.save(user=self.admin)

    def _node(self):
        response = self.query(
            'query ($id: ID) { appraisal(id: $id) { edges { node { status documentsComplete '
            'documents { documentType status required reviewedBy comment } '
            'returns { reason actionRequired isOpen requestedBy resubmittedBy } history { subject changes } } } } }',
            variables={"id": str(self.appraisal.id)}, headers={"HTTP_AUTHORIZATION": f"Bearer {self.token}"})
        return json.loads(response.content)["data"]["appraisal"]["edges"][0]["node"]

    def test_review_return_and_resubmit_over_graphql_and_in_the_history(self):
        self.send_mutation_raw(f'mutation {{ reviewApplicantDocument(input: {{appraisalId: "{self.appraisal.id}", '
                               f'documentId: "{self.document.id}", status: REJECTED, comment: "Blurred"}}) '
                               '{ internalId } }', self.token)
        self.send_mutation_raw(f'mutation {{ returnAppraisal(input: {{id: "{self.appraisal.id}", '
                               'reason: "ID unreadable", actionRequired: "Send a clear copy"}) { internalId } }',
                               self.token)
        self.send_mutation_raw(f'mutation {{ resubmitAppraisal(input: {{id: "{self.appraisal.id}", '
                               'note: "Clear copy received"}) { internalId } }', self.token)

        node = self._node()
        self.assertFalse(node["documentsComplete"])
        self.assertIn({"documentType": "doc_national_id", "status": "REJECTED", "required": True,
                       "reviewedBy": "appraisalDocsApi", "comment": "Blurred"}, node["documents"])
        self.assertEqual(node["returns"], [{"reason": "ID unreadable", "actionRequired": "Send a clear copy",
                                            "isOpen": False, "requestedBy": "appraisalDocsApi",
                                            "resubmittedBy": "appraisalDocsApi"}])
        subjects = [entry["subject"] for entry in node["history"]]
        self.assertIn("document:doc_national_id", subjects)
        self.assertEqual(subjects.count("return"), 2)
        resubmission = json.loads(next(e["changes"] for e in node["history"] if e["subject"] == "return"
                                       and "resubmitted_at" in e["changes"]))
        self.assertIn("resubmission_note", [change["field"] for change in resubmission])
