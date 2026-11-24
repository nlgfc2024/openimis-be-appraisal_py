import json

from django.core.exceptions import ValidationError
from django.test import TestCase
from tasks_management.models import Task
from tasks_management.services import TaskService

from appraisal import review, scoring
from appraisal.config import validate_programmes
from appraisal.models import Appraisal, AppraisalApproval, AppraisalItem
from appraisal.reports import appraisal_report
from appraisal.tests.test_review import RULES as CHECKLIST, make_appraisal
from appraisal.tests.test_scoring import RULES, ScoringTestCase, city_village
from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_admin_role, create_test_interactive_user
from individual.tests.test_helpers import create_individual

ApprovalStatus = AppraisalApproval.Status
SHORTLIST_APPROVAL = {**RULES, "approval": {"shortlist": True}}
DECISION_APPROVAL = {**CHECKLIST, "approval": {"eligibility": True}}


def complete(task_id, user, failed=False):
    """The supervisor approves (or rejects) the task, as the task group's completion policy does."""
    TaskService(user).complete_task({"id": task_id, "failed": failed})


class ApprovalConfigTest(TestCase):
    def _errors(self, programme):
        try:
            validate_programmes({"UMEP": {"source_types": ["kobo"], "round": "R1", **programme}})
        except ValidationError as exc:
            return exc.messages
        return []

    def test_approval_and_report_are_optional_and_checked(self):
        self.assertEqual(self._errors({"approval": {"shortlist": True, "eligibility": False},
                                       "report": {"breakdowns": [{"key": "sector", "label": "Sector",
                                                                  "field": "primary_sector"}]}}), [])
        self.assertEqual(self._errors({}), [])
        self.assertIn("programmes.UMEP.approval.shortlist must be true or false.",
                      self._errors({"approval": {"shortlist": "yes"}}))
        self.assertIn("programmes.UMEP.approval has unsupported keys: score.",
                      self._errors({"approval": {"score": True}}))
        self.assertIn("programmes.UMEP.report.breakdowns[0].field must be the application field to count by.",
                      self._errors({"report": {"breakdowns": [{"key": "sector", "label": "Sector"}]}}))


class ShortlistApprovalTest(ScoringTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.supervisor = create_test_interactive_user(username="appraisalSupervisor", roles=[create_admin_role().id])

    def setUp(self):
        self.appraisals = [scoring.score_appraisal(self._eligible({"jobs_to_create": str(jobs)},
                                                                  rules=SHORTLIST_APPROVAL, minutes=jobs), self.user)
                           for jobs in (12, 6, 1, 3)]

    def _statuses(self):
        return [Appraisal.objects.get(id=appraisal.id).status for appraisal in self.appraisals]

    def test_the_shortlist_waits_for_approval_then_takes_effect(self):
        summary = scoring.shortlist_round(self.user, "UMEP", "2026-R1")

        self.assertEqual(summary, {None: {"places": 3, "ranked": 4, "shortlisted": 3}})
        self.assertEqual(self._statuses(), ["SCORED"] * 4)
        self.assertFalse(any("desk_appraisal_status" in (a.individual.json_ext or {}) for a in self.appraisals))
        approval = AppraisalApproval.objects.get(kind="SHORTLIST")
        self.assertEqual((approval.status, approval.requested_by_id), (ApprovalStatus.PENDING, self.user.id))
        self.assertEqual((approval.summary["ranked"], approval.summary["shortlisted"]), (4, 3))
        self.assertEqual(str(approval), "Shortlist UMEP 2026-R1")
        task = Task.objects.get(id=approval.task_id)
        self.assertEqual((task.source, task.business_event, task.status),
                         ("appraisal", "appraisal_shortlist", Task.Status.RECEIVED))
        with self.assertRaisesMessage(ValidationError, "appraisal.approval_pending"):
            scoring.shortlist_round(self.user, "UMEP", "2026-R1")

        complete(task.id, self.supervisor)

        approval.refresh_from_db()
        self.assertEqual((approval.status, approval.decided_by_id), (ApprovalStatus.APPROVED, self.supervisor.id))
        self.assertEqual(self._statuses(), ["SHORTLISTED", "SHORTLISTED", "NOT_SHORTLISTED", "SHORTLISTED"])
        log = Appraisal.objects.get(id=self.appraisals[0].id).json_ext["shortlist"]
        self.assertEqual((log["by"], log["approved_by"]), (self.user.username, self.supervisor.username))

    def test_a_rejected_shortlist_changes_nothing(self):
        scoring.shortlist_round(self.user, "UMEP", "2026-R1")
        approval = AppraisalApproval.objects.get(kind="SHORTLIST")

        complete(approval.task_id, self.supervisor, failed=True)

        approval.refresh_from_db()
        self.assertEqual(approval.status, ApprovalStatus.REJECTED)
        self.assertEqual(self._statuses(), ["SCORED"] * 4)
        scoring.shortlist_round(self.user, "UMEP", "2026-R1")
        self.assertEqual(AppraisalApproval.objects.filter(status=ApprovalStatus.PENDING).count(), 1)

    def test_a_shortlist_whose_scores_changed_is_not_applied(self):
        scoring.shortlist_round(self.user, "UMEP", "2026-R1")
        approval = AppraisalApproval.objects.get(kind="SHORTLIST")
        jobs = AppraisalItem.objects.get(appraisal=self.appraisals[2], kind="SCORE", key="additional_jobs")
        scoring.answer_score(self.user, jobs.id, 25, "Hiring plan")

        complete(approval.task_id, self.supervisor)

        approval.refresh_from_db()
        self.assertEqual(approval.status, ApprovalStatus.FAILED)
        self.assertIn("appraisal.shortlist_changed", approval.outcome)
        self.assertEqual(Task.objects.get(id=approval.task_id).status, Task.Status.FAILED)
        self.assertEqual(self._statuses(), ["SCORED"] * 4)

    def test_an_approver_without_the_approve_right_changes_nothing(self):
        from appraisal.apps import RIGHT_APPRAISAL_VIEW
        from appraisal.tests.test_schema import _role

        role = _role("Appraisal viewer only", [RIGHT_APPRAISAL_VIEW], self.user)
        viewer = create_test_interactive_user(username="appraisalNotApprover", roles=[role.id])
        scoring.shortlist_round(self.user, "UMEP", "2026-R1")
        approval = AppraisalApproval.objects.get(kind="SHORTLIST")

        complete(approval.task_id, viewer)

        approval.refresh_from_db()
        self.assertEqual(approval.status, ApprovalStatus.FAILED)
        self.assertIn("appraisal.approver_not_allowed", approval.outcome)
        self.assertEqual(self._statuses(), ["SCORED"] * 4)

    def test_without_approval_the_shortlist_applies_at_once(self):
        Appraisal.objects.filter(id__in=[a.id for a in self.appraisals]).update(config_snapshot=RULES)
        scoring.shortlist_round(self.user, "UMEP", "2026-R1")
        self.assertFalse(AppraisalApproval.objects.exists())
        self.assertEqual(self._statuses().count("SHORTLISTED"), 3)


class DecisionApprovalTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.officer = create_test_interactive_user(username="appraisalDecider", roles=[create_admin_role().id])
        cls.supervisor = create_test_interactive_user(username="appraisalChecker", roles=[create_admin_role().id])

    def setUp(self):
        self.appraisal, self.items = make_appraisal(self.officer, results=("YES", "YES", "NO"))
        Appraisal.objects.filter(id=self.appraisal.id).update(config_snapshot=DECISION_APPROVAL)
        review.answer_item(self.officer, self.items["co_financing"].id, "NO", "Grant request template")

    def _reload(self):
        return Appraisal.objects.get(id=self.appraisal.id)

    def test_the_decision_waits_for_approval_and_locks_the_checklist(self):
        review.decide_eligibility(self.officer, self.appraisal.id, "No co-financing")

        self.assertEqual((self._reload().status, self._reload().eligibility), ("DESK_SCREENING", None))
        approval = AppraisalApproval.objects.get(kind="ELIGIBILITY", appraisal=self.appraisal)
        self.assertEqual((approval.summary["decision"], approval.summary["comment"]),
                         ("INELIGIBLE", "No co-financing"))
        for change in (lambda: review.answer_item(self.officer, self.items["sector"].id, "YES", "x"),
                       lambda: review.decide_eligibility(self.officer, self.appraisal.id)):
            with self.subTest(), self.assertRaisesMessage(ValidationError, "appraisal.approval_pending"):
                change()

        complete(approval.task_id, self.supervisor)

        appraisal = self._reload()
        self.assertEqual((appraisal.status, appraisal.eligibility, appraisal.stage),
                         ("INELIGIBLE", "INELIGIBLE", "CLOSED"))
        entry = appraisal.json_ext["eligibility_decisions"][-1]
        self.assertEqual((entry["by"], entry["approved_by"], entry["comment"]),
                         (self.officer.username, self.supervisor.username, "No co-financing"))
        with self.assertRaisesMessage(ValidationError, "appraisal.screening_closed"):
            review.answer_item(self.officer, self.items["sector"].id, "YES", "x")

    def test_a_decision_whose_answers_changed_is_not_applied(self):
        review.decide_eligibility(self.officer, self.appraisal.id)
        approval = AppraisalApproval.objects.get(kind="ELIGIBILITY")
        AppraisalItem.objects.filter(id=self.items["sector"].id).update(basis_changed=True)

        complete(approval.task_id, self.supervisor)

        approval.refresh_from_db()
        self.assertEqual(approval.status, ApprovalStatus.FAILED)
        self.assertIn("appraisal.answers_changed", approval.outcome)
        self.assertEqual(self._reload().status, "DESK_SCREENING")

    def test_a_rejected_decision_reopens_the_checklist(self):
        review.decide_eligibility(self.officer, self.appraisal.id)
        complete(AppraisalApproval.objects.get(kind="ELIGIBILITY").task_id, self.supervisor, failed=True)

        self.assertEqual(self._reload().eligibility, None)
        review.answer_item(self.officer, self.items["co_financing"].id, "YES", "Bank statement")
        review.decide_eligibility(self.officer, self.appraisal.id)
        self.assertEqual(AppraisalApproval.objects.filter(status=ApprovalStatus.PENDING).count(), 1)


class ReportTest(ScoringTestCase):
    def test_counts_by_status_city_breakdown_and_what_awaits_action(self):
        rules = {**RULES, "shortlist": {"per_city": {"Blantyre": 1, "Zomba": 1}},
                 "report": {"breakdowns": [{"key": "sector", "label": "Sector", "field": "primary_sector"}]}}
        blantyre, zomba = city_village("Blantyre"), city_village("Zomba")
        appraisals = [self._eligible({"jobs_to_create": str(jobs)}, rules=rules, location=place, minutes=jobs)
                      for jobs, place in ((12, blantyre), (6, blantyre), (3, zomba))]
        for appraisal, sector in zip(appraisals, ("agriculture", "energy", "agriculture")):
            appraisal.individual.json_ext = {**(appraisal.individual.json_ext or {}), "primary_sector": sector}
            appraisal.individual.save(user=self.user)
            scoring.score_appraisal(appraisal, self.user)
        scoring.shortlist_round(self.user, "UMEP", "2026-R1")
        returned = self._eligible(rules=rules, location=zomba)
        Appraisal.objects.filter(id=returned.id).update(status="RETURNED", eligibility=None)
        received = self._eligible(rules=rules, location=zomba)
        Appraisal.objects.filter(id=received.id).update(status="RECEIVED", eligibility=None)

        report = appraisal_report(self.user, "UMEP")

        self.assertEqual((report["round"], report["total"]), ("2026-R1", 5))
        self.assertEqual({row["key"]: row["count"] for row in report["by_status"]},
                         {"SHORTLISTED": 2, "NOT_SHORTLISTED": 1, "RETURNED": 1, "RECEIVED": 1})
        cities = {row["city"]: row for row in report["by_city"]}
        self.assertEqual({city: (row["total"], row["eligible"], row["shortlisted"], row["places"])
                          for city, row in cities.items()},
                         {"Blantyre": (2, 2, 1, 1), "Zomba": (3, 1, 1, 1)})
        self.assertEqual(report["breakdowns"][0]["counts"],
                         [{"key": "agriculture", "count": 2}, {"key": "", "count": 2}, {"key": "energy", "count": 1}])
        self.assertEqual((report["awaiting"]["returned"], report["awaiting"]["not_started"]), (1, 1))
        self.assertIsNone(appraisal_report(self.user, "OTHER"))


class ApprovalApiTest(openIMISGraphQLTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = create_test_interactive_user(username="appraisalApprovalApi", roles=[create_admin_role().id])
        cls.token = BaseTestContext(user=cls.admin).get_jwt()

    def test_approvals_and_report_over_graphql(self):
        appraisal = Appraisal(individual=create_individual(self.admin.username), programme="UMEP", round="2026-R1",
                              config_snapshot=SHORTLIST_APPROVAL, status="SCORED", stage="DESK_SCORING",
                              eligibility="ELIGIBLE", score=80)
        appraisal.save(user=self.admin)
        self.send_mutation_raw('mutation { shortlistAppraisals(input: {programme: "UMEP", round: "2026-R1"}) '
                               '{ internalId } }', self.token)
        headers = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}
        response = self.query('{ appraisalApprovals(programme: "UMEP", kind: "SHORTLIST") '
                              '{ kind status requestedBy summary taskId } '
                              'appraisalReport(programme: "UMEP") { round total byStatus { key count } '
                              'awaiting { shortlistApproval } documents { incomplete } } }', headers=headers)
        data = json.loads(response.content)["data"]
        approval = data["appraisalApprovals"][0]
        self.assertEqual((approval["kind"], approval["status"], approval["requestedBy"]),
                         ("SHORTLIST", "PENDING", self.admin.username))
        self.assertEqual(json.loads(approval["summary"])["shortlisted"], 1)
        self.assertIsNotNone(approval["taskId"])
        report = data["appraisalReport"]
        self.assertEqual((report["round"], report["total"], report["byStatus"]),
                         ("2026-R1", 1, [{"key": "SCORED", "count": 1}]))
        self.assertEqual(report["awaiting"]["shortlistApproval"], 1)
