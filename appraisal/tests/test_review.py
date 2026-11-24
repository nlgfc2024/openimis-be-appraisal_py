from django.core.exceptions import ValidationError
from django.test import TestCase

from appraisal import review
from appraisal.models import Appraisal, AppraisalItem
from core.test_helpers import create_admin_role, create_test_interactive_user
from individual.tests.test_helpers import create_individual

Result, Status, Stage, Eligibility = AppraisalItem.Result, Appraisal.Status, Appraisal.Stage, Appraisal.Eligibility
RULES = {"eligibility": [{"key": "youth_age", "label": "Youth aged 18-35"}, {"key": "sector", "label": "Sector"},
                         {"key": "co_financing", "label": "Co-financing"}]}


def make_appraisal(user, individual=None, results=("YES", "YES", "PENDING")):
    """An appraisal with the RULES checklist, each item's system result taken from ``results``."""
    appraisal = Appraisal(individual=individual or create_individual(user.username), programme="UMEP",
                          round="2026-R1", config_snapshot=RULES)
    appraisal.save(user=user)
    items = {}
    for rule, result in zip(RULES["eligibility"], results):
        item = AppraisalItem(appraisal=appraisal, kind="ELIGIBILITY", key=rule["key"], system_result=result,
                             basis={"note": "test"})
        item.save(user=user)
        items[rule["key"]] = item
    return appraisal, items


class ReviewTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.officer = create_test_interactive_user(username="appraisalScreener", roles=[create_admin_role().id])

    def setUp(self):
        self.appraisal, self.items = make_appraisal(self.officer)

    def _reload(self):
        return Appraisal.objects.get(id=self.appraisal.id)


class AnswerItemTest(ReviewTestCase):
    def test_an_answer_is_recorded_against_the_officer_and_opens_screening(self):
        item = review.answer_item(self.officer, self.items["co_financing"].id, "YES", " Grant request template ")

        self.assertEqual((item.officer_result, item.result, item.evidence), ("YES", "YES", "Grant request template"))
        self.assertEqual(item.decided_by_id, self.officer.id)
        self.assertIsNotNone(item.decided_at)
        self.assertEqual((self._reload().stage, self._reload().status), (Stage.DESK_SCREENING, Status.DESK_SCREENING))

    def test_yes_and_no_need_evidence_and_pending_a_clarification(self):
        for result, message in (("YES", "appraisal.evidence_required"), ("NO", "appraisal.evidence_required"),
                                ("PENDING", "appraisal.clarification_required")):
            with self.subTest(result=result), self.assertRaisesMessage(ValidationError, message):
                review.answer_item(self.officer, self.items["sector"].id, result, "", " ")
        item = review.answer_item(self.officer, self.items["sector"].id, "PENDING", comment="Which sector?")
        self.assertEqual(item.result, "PENDING")

    def test_answering_again_clears_a_changed_basis(self):
        AppraisalItem.objects.filter(id=self.items["sector"].id).update(basis_changed=True)
        item = review.answer_item(self.officer, self.items["sector"].id, "YES", "Registration certificate")
        self.assertFalse(item.basis_changed)

    def test_only_eligibility_items_of_an_open_screening(self):
        score = AppraisalItem(appraisal=self.appraisal, kind="SCORE", key="jobs", system_points=8)
        score.save(user=self.officer)
        with self.assertRaisesMessage(ValidationError, "appraisal.item_not_eligibility"):
            review.answer_item(self.officer, score.id, "YES", "x")
        Appraisal.objects.filter(id=self.appraisal.id).update(status=Status.INELIGIBLE, stage=Stage.CLOSED)
        with self.assertRaisesMessage(ValidationError, "appraisal.screening_closed"):
            review.answer_item(self.officer, self.items["sector"].id, "YES", "x")

    def test_an_unknown_result_or_item_is_refused(self):
        with self.assertRaisesMessage(ValidationError, "appraisal.invalid_result"):
            review.answer_item(self.officer, self.items["sector"].id, "MAYBE", "x")
        with self.assertRaisesMessage(ValidationError, "appraisal.item_not_found"):
            review.answer_item(self.officer, self.appraisal.id, "YES", "x")


class DecideEligibilityTest(ReviewTestCase):
    def test_pending_items_ask_for_clarification_and_keep_screening_open(self):
        appraisal = review.decide_eligibility(self.officer, self.appraisal.id, "Template unreadable")

        self.assertEqual((appraisal.eligibility, appraisal.status, appraisal.stage),
                         (Eligibility.CLARIFICATION, Status.DESK_SCREENING, Stage.DESK_SCREENING))
        decision = appraisal.json_ext["eligibility_decisions"][-1]
        self.assertEqual((decision["decision"], decision["pending"], decision["by"], decision["comment"]),
                         ("CLARIFICATION", ["co_financing"], "appraisalScreener", "Template unreadable"))

    def test_all_yes_is_eligible_and_moves_to_scoring(self):
        review.answer_item(self.officer, self.items["co_financing"].id, "YES", "Template: 32%")
        appraisal = review.decide_eligibility(self.officer, self.appraisal.id)
        self.assertEqual((appraisal.eligibility, appraisal.status, appraisal.stage),
                         (Eligibility.ELIGIBLE, Status.ELIGIBLE, Stage.DESK_SCORING))

    def test_any_no_is_ineligible_even_with_pending_items(self):
        review.answer_item(self.officer, self.items["youth_age"].id, "NO", "National ID: born 1980")
        appraisal = review.decide_eligibility(self.officer, self.appraisal.id)
        self.assertEqual((appraisal.eligibility, appraisal.status, appraisal.stage),
                         (Eligibility.INELIGIBLE, Status.INELIGIBLE, Stage.CLOSED))
        self.assertEqual(appraisal.json_ext["eligibility_decisions"][-1]["ineligible"], ["youth_age"])

    def test_a_decided_appraisal_is_closed_to_further_decisions(self):
        review.answer_item(self.officer, self.items["youth_age"].id, "NO", "ID")
        review.decide_eligibility(self.officer, self.appraisal.id)
        with self.assertRaisesMessage(ValidationError, "appraisal.screening_closed"):
            review.decide_eligibility(self.officer, self.appraisal.id)

    def test_every_rule_needs_an_item_and_changed_answers_need_review(self):
        self.items["sector"].delete(user=self.officer)
        with self.assertRaisesMessage(ValidationError, "appraisal.checklist_incomplete: sector"):
            review.decide_eligibility(self.officer, self.appraisal.id)
        appraisal, items = make_appraisal(self.officer)
        AppraisalItem.objects.filter(id=items["youth_age"].id).update(basis_changed=True)
        with self.assertRaisesMessage(ValidationError, "appraisal.answers_changed: review youth_age again"):
            review.decide_eligibility(self.officer, appraisal.id)

    def test_the_decision_is_kept_in_the_history(self):
        review.decide_eligibility(self.officer, self.appraisal.id)
        review.answer_item(self.officer, self.items["co_financing"].id, "YES", "Template: 40%")
        review.decide_eligibility(self.officer, self.appraisal.id)

        appraisal = self._reload()
        self.assertEqual([d["decision"] for d in appraisal.json_ext["eligibility_decisions"]],
                         ["CLARIFICATION", "ELIGIBLE"])
        self.assertIn("ELIGIBLE", [h.status for h in appraisal.history.all()])


class ReopenTest(ReviewTestCase):
    def test_a_decision_is_cleared_and_screening_done_again(self):
        review.answer_item(self.officer, self.items["youth_age"].id, "NO", "National ID: born 1980")
        review.decide_eligibility(self.officer, self.appraisal.id)

        appraisal = review.reopen_screening(self.officer, self.appraisal.id, " Born 1998, misread ")

        self.assertEqual((appraisal.status, appraisal.stage, appraisal.eligibility),
                         (Status.DESK_SCREENING, Stage.DESK_SCREENING, None))
        entry = appraisal.json_ext["reopened"][-1]
        self.assertEqual((entry["from"], entry["by"], entry["reason"]),
                         ("INELIGIBLE", "appraisalScreener", "Born 1998, misread"))
        review.answer_item(self.officer, self.items["youth_age"].id, "YES", "National ID: born 1998")
        review.answer_item(self.officer, self.items["co_financing"].id, "YES", "Template: 40%")
        self.assertEqual(review.decide_eligibility(self.officer, self.appraisal.id).eligibility, Eligibility.ELIGIBLE)

    def test_only_a_decided_appraisal_with_a_reason(self):
        with self.assertRaisesMessage(ValidationError, "appraisal.not_reopenable: the appraisal is application"):
            review.reopen_screening(self.officer, self.appraisal.id, "x")
        review.answer_item(self.officer, self.items["youth_age"].id, "NO", "ID")
        review.decide_eligibility(self.officer, self.appraisal.id)
        with self.assertRaisesMessage(ValidationError, "appraisal.reason_required"):
            review.reopen_screening(self.officer, self.appraisal.id, "  ")
