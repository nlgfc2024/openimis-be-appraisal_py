import json
from unittest.mock import patch

from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.models import Role, RoleRight
from core.utils import TimeUtils
from core.test_helpers import create_admin_role, create_test_interactive_user
from individual.tests.test_helpers import create_individual, create_sp_role
from location.test_helpers import assign_user_districts, create_test_village

from appraisal.apps import AppraisalConfig, RIGHT_APPRAISAL_RETURN, RIGHT_APPRAISAL_SCREEN, RIGHT_APPRAISAL_VIEW
from appraisal.models import Appraisal, AppraisalItem
from appraisal.tests.test_review import make_appraisal

INDIVIDUAL_SEARCH = 159001

APPRAISALS = """
query ($search: String, $answers: [String]) {
  appraisal(programme: "UMEP", search: $search, answers: $answers, orderBy: ["individual__last_name"]) {
    totalCount
    edges { node {
      uuid programme round stage status eligibility koboId
      individual { firstName lastName }
      items(kind: "ELIGIBILITY") { uuid key label systemResult result isOverridden basis decidedBy }
      history { subject user changes }
    } }
  }
}"""


def _role(name, rights, creator):
    role = Role.objects.create(name=name, is_blocked=False, is_system=False, audit_user_id=creator.id_for_audit)
    for right in rights:
        RoleRight.objects.create(role_id=role.id, right_id=right, audit_user_id=role.audit_user_id,
                                 validity_from=TimeUtils.now())
    return role


class AppraisalApiTest(openIMISGraphQLTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = create_test_interactive_user(username="appraisalApiAdmin", roles=[create_admin_role().id])
        cls.admin_token = BaseTestContext(user=cls.admin).get_jwt()
        village_a = create_test_village({"name": "Api village A", "code": "ApiVA"})
        village_b = create_test_village({"name": "Api village B", "code": "ApiVB"})
        cls.grace = create_individual(cls.admin.username, {
            "first_name": "Grace", "last_name": "Banda", "location": village_a,
            "json_ext": {"national_id": "AB12CD34", "primary_sector": "energy"}})
        cls.john = create_individual(cls.admin.username, {
            "first_name": "John", "last_name": "Phiri", "location": village_b,
            "json_ext": {"national_id": "XY98ZW76", "primary_sector": "tourism", "jobs_to_create": 5}})
        cls.screener = create_test_interactive_user(
            username="appraisalApiScreener",
            roles=[_role("Appraisal screener", [RIGHT_APPRAISAL_VIEW, RIGHT_APPRAISAL_SCREEN, INDIVIDUAL_SEARCH],
                         cls.admin).id])
        assign_user_districts(cls.screener, [village_a.parent.parent.code])
        cls.screener_token = BaseTestContext(user=cls.screener).get_jwt()
        cls.viewer = create_test_interactive_user(
            username="appraisalApiViewer",
            roles=[_role("Appraisal viewer", [RIGHT_APPRAISAL_VIEW, INDIVIDUAL_SEARCH], cls.admin).id])
        assign_user_districts(cls.viewer, [village_a.parent.parent.code])
        cls.viewer_token = BaseTestContext(user=cls.viewer).get_jwt()
        cls.appraisal_only = create_test_interactive_user(
            username="appraisalApiOnly", roles=[_role("Appraisal only", [RIGHT_APPRAISAL_VIEW], cls.admin).id])
        cls.appraisal_only_token = BaseTestContext(user=cls.appraisal_only).get_jwt()
        cls.reopener = create_test_interactive_user(
            username="appraisalApiReopener",
            roles=[_role("Appraisal reopener", [RIGHT_APPRAISAL_VIEW, INDIVIDUAL_SEARCH, RIGHT_APPRAISAL_RETURN],
                         cls.admin).id])
        assign_user_districts(cls.reopener, [village_a.parent.parent.code])
        cls.reopener_token = BaseTestContext(user=cls.reopener).get_jwt()
        cls.nobody = create_test_interactive_user(username="appraisalApiNobody", roles=[create_sp_role(cls.admin).id])
        cls.nobody_token = BaseTestContext(user=cls.nobody).get_jwt()

    def setUp(self):
        super().setUp()
        # Whatever the database's saved configuration, these tests run as configured
        configured = patch.object(AppraisalConfig, "config_error", None)
        configured.start()
        self.addCleanup(configured.stop)
        self.appraisal_a, self.items_a = make_appraisal(self.admin, self.grace)
        self.appraisal_b, _ = make_appraisal(self.admin, self.john)

    def _appraisals(self, token, **variables):
        response = self.query(APPRAISALS, variables=variables, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"})
        return json.loads(response.content)

    def test_the_list_shows_each_appraisal_with_its_checklist_in_rule_order(self):
        content = self._appraisals(self.admin_token)

        self.assertNotIn("errors", content)
        nodes = [edge["node"] for edge in content["data"]["appraisal"]["edges"]]
        self.assertEqual([n["individual"]["lastName"] for n in nodes], ["Banda", "Phiri"])
        items = nodes[0]["items"]
        self.assertEqual([(i["key"], i["label"], i["result"]) for i in items],
                         [("youth_age", "Youth aged 18-35", "YES"), ("sector", "Sector", "YES"),
                          ("co_financing", "Co-financing", "PENDING")])
        self.assertEqual(json.loads(items[0]["basis"]), {"note": "test"})

    def test_search_and_answer_filters(self):
        cases = [({"search": "ab12"}, ["Banda"]), ({"search": "phiri"}, ["Phiri"]),
                 ({"answers": ["primary_sector=tourism"]}, ["Phiri"]), ({"answers": ["jobs_to_create=5"]}, ["Phiri"]),
                 ({"answers": ["jobs_to_create=5.0"]}, ["Phiri"]), ({"answers": ["jobs_to_create=6"]}, [])]
        for variables, expected in cases:
            with self.subTest(variables=variables):
                nodes = self._appraisals(self.admin_token, **variables)["data"]["appraisal"]["edges"]
                self.assertEqual([n["node"]["individual"]["lastName"] for n in nodes], expected)

    def test_the_list_follows_location_rights_and_needs_the_view_right(self):
        nodes = self._appraisals(self.screener_token)["data"]["appraisal"]["edges"]
        self.assertEqual([n["node"]["individual"]["lastName"] for n in nodes], ["Banda"])
        content = self._appraisals(self.nobody_token)
        self.assertEqual(content["errors"][0]["message"], "unauthorized")

    def test_an_invalid_saved_configuration_is_reported(self):
        with patch.object(AppraisalConfig, "config_error", "programmes.UMEP.round must be a non-empty string."):
            content = self._appraisals(self.admin_token)
            self.assertEqual(content["errors"][0]["message"],
                             "['appraisal.not_configured: programmes.UMEP.round must be a non-empty string.']")
            with self.assertRaisesRegex(ValueError, "appraisal.not_configured"):
                self.send_mutation_raw(f'''mutation {{ decideAppraisalEligibility(input: {{id: "{self.appraisal_a.id}"}})
                    {{ internalId }} }}''', self.admin_token)

    def test_the_page_is_told_what_the_user_may_do(self):
        query = """query ($id: ID) { appraisal(id: $id) { edges { node { canScreen canDecide canReopen
            decisionPreview { decision pending unanswered changed problem } } } } }"""

        def node(token):
            response = self.query(query, variables={"id": str(self.appraisal_a.id)},
                                  headers={"HTTP_AUTHORIZATION": f"Bearer {token}"})
            return json.loads(response.content)["data"]["appraisal"]["edges"][0]["node"]

        screener = node(self.screener_token)
        self.assertEqual((screener["canScreen"], screener["canDecide"], screener["canReopen"]), (True, True, False))
        self.assertEqual(screener["decisionPreview"], {"decision": "CLARIFICATION", "pending": ["co_financing"],
                                                       "unanswered": [], "changed": [], "problem": None})
        viewer = node(self.viewer_token)
        self.assertEqual((viewer["canScreen"], viewer["canDecide"]), (False, False))
        Appraisal.objects.filter(id=self.appraisal_a.id).update(status="INELIGIBLE")
        self.assertEqual((node(self.screener_token)["canScreen"], node(self.reopener_token)["canReopen"]),
                         (False, True))

    def test_applicant_data_needs_the_individual_search_right_too(self):
        content = self._appraisals(self.appraisal_only_token)
        self.assertEqual(content["errors"][0]["message"], "unauthorized")
        self.assertNotIn("errors", self._appraisals(self.viewer_token))

    def test_a_decision_is_reopened_with_the_return_right_and_a_reason(self):
        Appraisal.objects.filter(id=self.appraisal_a.id).update(status="INELIGIBLE", eligibility="INELIGIBLE",
                                                                stage="CLOSED")
        mutation = f'''mutation {{ reopenAppraisal(input: {{id: "{self.appraisal_a.id}",
            reason: "Age misread from the ID"}}) {{ internalId }} }}'''
        with self.assertRaisesRegex(ValueError, "unauthorized"):
            self.send_mutation_raw(mutation, self.screener_token)

        self.send_mutation_raw(mutation, self.reopener_token)

        appraisal = Appraisal.objects.get(id=self.appraisal_a.id)
        self.assertEqual((appraisal.status, appraisal.eligibility), ("DESK_SCREENING", None))
        self.assertEqual(appraisal.json_ext["reopened"][0]["reason"], "Age misread from the ID")

    def test_a_screener_answers_and_decides_and_the_history_shows_it(self):
        self.send_mutation_raw(
            f'''mutation {{ answerAppraisalItem(input: {{id: "{self.items_a["co_financing"].id}", result: YES,
                evidence: "Grant request: 32%", clientMutationLabel: "answer"}}) {{ internalId }} }}''',
            self.screener_token)
        self.send_mutation_raw(
            f'''mutation {{ decideAppraisalEligibility(input: {{id: "{self.appraisal_a.id}",
                clientMutationLabel: "decide"}}) {{ internalId }} }}''', self.screener_token)

        appraisal = Appraisal.objects.get(id=self.appraisal_a.id)
        self.assertEqual((appraisal.eligibility, appraisal.status), ("ELIGIBLE", "ELIGIBLE"))
        item = AppraisalItem.objects.get(id=self.items_a["co_financing"].id)
        self.assertEqual((item.officer_result, item.decided_by_id), ("YES", self.screener.id))
        node = self._appraisals(self.screener_token, search="Banda")["data"]["appraisal"]["edges"][0]["node"]
        history = [(h["subject"], h["user"]) for h in node["history"]]
        self.assertIn(("co_financing", "appraisalApiScreener"), history)
        self.assertIn(("appraisal", "appraisalApiScreener"), history)
        changes = json.loads(next(h for h in node["history"] if h["subject"] == "co_financing")["changes"])
        self.assertIn({"field": "officer_result", "old": None, "new": "YES"}, changes)

    def test_screening_needs_the_screen_right_and_the_appraisal_in_reach(self):
        mutation = f'''mutation {{ answerAppraisalItem(input: {{id: "{self.items_a["sector"].id}", result: NO,
            evidence: "x"}}) {{ internalId }} }}'''
        with self.assertRaisesRegex(ValueError, "unauthorized"):
            self.send_mutation_raw(mutation, self.viewer_token)
        other = f'''mutation {{ decideAppraisalEligibility(input: {{id: "{self.appraisal_b.id}"}}) {{ internalId }} }}'''
        with self.assertRaisesRegex(ValueError, "appraisal.not_found"):
            self.send_mutation_raw(other, self.screener_token)
        self.assertIsNone(AppraisalItem.objects.get(id=self.items_a["sector"].id).officer_result)
        self.assertIsNone(Appraisal.objects.get(id=self.appraisal_b.id).eligibility)

    def test_one_appraisal_by_its_uuid_as_the_frontend_asks(self):
        query = """query ($id: ID) { appraisal(id: $id, first: 1) { totalCount edges { node { uuid } } } }"""
        response = self.query(query, variables={"id": str(self.appraisal_a.id)},
                              headers={"HTTP_AUTHORIZATION": f"Bearer {self.admin_token}"})
        content = json.loads(response.content)
        self.assertNotIn("errors", content)
        self.assertEqual([e["node"]["uuid"] for e in content["data"]["appraisal"]["edges"]], [str(self.appraisal_a.id)])
