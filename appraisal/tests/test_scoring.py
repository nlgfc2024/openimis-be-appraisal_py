import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from appraisal import review, scoring
from appraisal.config import validate_programmes
from appraisal.models import Appraisal, AppraisalItem
from core.models.openimis_graphql_test_case import BaseTestContext, openIMISGraphQLTestCase
from core.test_helpers import create_admin_role, create_test_interactive_user
from individual.models import Individual
from individual.tests.test_helpers import create_individual
from location.models import Location
from msr_etl.models import KoboSubmission

# The Annex 2B rubric as configured for UMEP (coremis-configs appraisal/umep.appraisal.config.json)
UMEP_SCORING = {"normalise_to": 100, "components": [
    {"key": "operation_duration", "label": "Business operation duration", "field": "months_in_operation",
     "weight": 15, "scoring": {"type": "numeric_bands", "bands": [
         {"min": 12, "max": 18, "points": 9}, {"min": 18, "max": 24, "points": 12}, {"min": 24, "points": 15}]}},
    {"key": "annual_turnover", "label": "Annual business turnover", "field": "turnover_past_12m_mwk", "weight": 15,
     "scoring": {"type": "numeric_bands", "bands": [
         {"min": 3000000, "max": 5000000, "points": 12}, {"min": 5000000, "points": 15}]}},
    {"key": "business_bank_account", "label": "Business operational bank account",
     "field": "has_business_bank_account", "weight": 10,
     "scoring": {"type": "mapping", "values": {"true": 10}, "default": 0}},
    {"key": "additional_jobs", "label": "Ability to create additional jobs", "field": "jobs_to_create", "weight": 25,
     "scoring": {"type": "numeric_bands", "bands": [
         {"min": 1, "max": 2, "points": 8}, {"min": 2, "max": 5, "points": 13}, {"min": 5, "max": 10, "points": 19},
         {"min": 10, "points": 25}]}},
    {"key": "bank_statement", "label": "Financial records / bank statement evidence", "field": "has_bank_statement",
     "weight": 10, "scoring": {"type": "mapping", "values": {"true": 10}, "default": 0}},
    {"key": "stable_market", "label": "Availability of stable market", "field": "market_stability", "weight": 25,
     "scoring": {"type": "mapping", "values": {"stable": 25, "irregular": 12}, "default": 0}},
]}
BEST = {"months_in_operation": 30, "turnover_past_12m_mwk": "6000000", "has_business_bank_account": True,
        "jobs_to_create": "12", "has_bank_statement": True, "market_stability": "stable"}
RULES = {"source_types": ["kobo"], "round": "2026-R1",
         "eligibility": [{"key": "youth_age", "label": "Youth aged 18-35"}], "scoring": UMEP_SCORING,
         "shortlist": {"size": 3}}
SUBMITTED = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)


def city_village(name):
    """A village under a city (type R) called ``name``."""
    city = Location.objects.filter(type="R", name=name, validity_to__isnull=True).first() or \
        Location.objects.create(type="R", code=f"T{name[:6]}", name=name, audit_user_id=-1)
    place = city
    for kind in ("D", "W", "V"):
        place = Location.objects.create(type=kind, code=f"{city.code}{kind}{Location.objects.count()}",
                                        name=f"{name} {kind}", parent=place, audit_user_id=-1)
    return place


class ScoringTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_interactive_user(username="appraisalScorer", roles=[create_admin_role().id])

    def _eligible(self, inputs=None, rules=RULES, location=None, minutes=0, kobo_id=None):
        """An eligible appraisal with these score inputs and an application submitted ``minutes`` after SUBMITTED."""
        individual = create_individual(self.user.username, {"location": location} if location else {})
        kobo_id = kobo_id or (KoboSubmission.objects.count() + 1)
        submission = KoboSubmission.objects.create(
            source_type="kobo", asset_uid="aForm", kobo_id=kobo_id, data={},
            submitted_at=SUBMITTED + timedelta(minutes=minutes), individual=individual, status="IMPORTED")
        appraisal = Appraisal(individual=individual, programme="UMEP", round="2026-R1", submission=submission,
                              config_snapshot=rules, status=Appraisal.Status.ELIGIBLE,
                              stage=Appraisal.Stage.DESK_SCORING, eligibility=Appraisal.Eligibility.ELIGIBLE,
                              json_ext={"score_inputs": {**BEST, **(inputs or {})}})
        appraisal.save(user=self.user)
        return appraisal

    def _items(self, appraisal):
        return {item.key: item for item in appraisal.items.filter(kind="SCORE", is_deleted=False)}


class ScoreTest(ScoringTestCase):
    def test_the_best_application_scores_100(self):
        appraisal = scoring.score_appraisal(self._eligible(), self.user)

        self.assertEqual((appraisal.status, appraisal.stage, appraisal.score),
                         ("SCORED", "DESK_SCORING", Decimal("100.00")))
        points = {key: item.system_points for key, item in self._items(appraisal).items()}
        self.assertEqual(points, {"operation_duration": 15, "annual_turnover": 15, "business_bank_account": 10,
                                  "additional_jobs": 25, "bank_statement": 10, "stable_market": 25})

    def test_annex_2b_bands(self):
        appraisal = scoring.score_appraisal(self._eligible({
            "months_in_operation": 18, "turnover_past_12m_mwk": "5000000", "has_business_bank_account": False,
            "jobs_to_create": "6", "has_bank_statement": False, "market_stability": "irregular"}), self.user)

        points = {key: item.system_points for key, item in self._items(appraisal).items()}
        self.assertEqual(points, {"operation_duration": 9, "annual_turnover": 12, "business_bank_account": 0,
                                  "additional_jobs": 19, "bank_statement": 0, "stable_market": 12})
        self.assertEqual(appraisal.score, Decimal("52.00"))

    def test_missing_or_unreadable_inputs_score_the_default_and_say_why(self):
        appraisal = scoring.score_appraisal(
            self._eligible({"months_in_operation": None, "jobs_to_create": "many"}), self.user)

        items = self._items(appraisal)
        self.assertEqual((items["operation_duration"].system_points, items["operation_duration"].basis["note"]),
                         (0, "No answer; an officer decides"))
        self.assertEqual((items["additional_jobs"].system_points, items["additional_jobs"].basis["note"]),
                         (0, "'many' can't be scored; an officer decides"))
        self.assertEqual(appraisal.score, Decimal("60.00"))

    def test_weights_other_than_the_maximum_points_are_normalised(self):
        rules = {**RULES, "scoring": {"normalise_to": 100, "components": [
            {"key": "a", "label": "A", "field": "x", "weight": 1, "scoring": {"type": "mapping", "values": {"y": 4}}},
            {"key": "b", "label": "B", "field": "z", "weight": 3, "scoring": {"type": "mapping", "values": {"y": 2}}},
        ]}}
        appraisal = scoring.score_appraisal(self._eligible({"x": "y", "z": "n"}, rules=rules), self.user)
        self.assertEqual(appraisal.score, Decimal("25.00"))

    def test_only_eligible_or_scored_appraisals_are_scored(self):
        appraisal = self._eligible()
        Appraisal.objects.filter(id=appraisal.id).update(status="DESK_SCREENING")
        appraisal.refresh_from_db()
        with self.assertRaisesMessage(ValidationError, "appraisal.not_scorable"):
            scoring.score_appraisal(appraisal, self.user)

    def test_an_eligible_decision_scores_at_once(self):
        appraisal = self._eligible()
        Appraisal.objects.filter(id=appraisal.id).update(status="DESK_SCREENING", stage="DESK_SCREENING",
                                                         eligibility=None)
        AppraisalItem(appraisal=appraisal, kind="ELIGIBILITY", key="youth_age", system_result="YES").save(
            user=self.user)

        appraisal = review.decide_eligibility(self.user, appraisal.id)

        self.assertEqual((appraisal.eligibility, appraisal.status, appraisal.score), ("ELIGIBLE", "SCORED", 100))


class OfficerScoreTest(ScoringTestCase):
    def setUp(self):
        self.appraisal = scoring.score_appraisal(self._eligible({"market_stability": None}), self.user)
        self.market = self._items(self.appraisal)["stable_market"]

    def test_an_officer_sets_points_with_evidence_and_the_total_follows(self):
        item = scoring.answer_score(self.user, self.market.id, "12", "Site visit: seasonal sales")

        self.appraisal.refresh_from_db()
        self.assertEqual((item.officer_points, item.points, item.decided_by_id), (12, 12, self.user.id))
        self.assertEqual(self.appraisal.score, Decimal("87.00"))

    def test_points_must_be_within_the_component_and_evidenced(self):
        for points, evidence, message in (("26", "x", "appraisal.invalid_points: from 0 to 25.00"),
                                          ("-1", "x", "appraisal.invalid_points"),
                                          ("abc", "x", "appraisal.invalid_points"),
                                          ("12", " ", "appraisal.evidence_required")):
            with self.subTest(points=points), self.assertRaisesMessage(ValidationError, message):
                scoring.answer_score(self.user, self.market.id, points, evidence)

    def test_rescoring_keeps_the_officer_points_and_flags_a_changed_basis(self):
        scoring.answer_score(self.user, self.market.id, "12", "Site visit")
        Appraisal.objects.filter(id=self.appraisal.id).update(
            json_ext={"score_inputs": {**BEST, "market_stability": "stable"}})
        self.appraisal.refresh_from_db()

        scoring.score_appraisal(self.appraisal, self.user)

        item = AppraisalItem.objects.get(id=self.market.id)
        self.assertEqual((item.system_points, item.officer_points, item.basis_changed), (25, 12, True))
        self.assertEqual(Appraisal.objects.get(id=self.appraisal.id).score, Decimal("87.00"))

    def test_an_unchanged_rescore_writes_nothing(self):
        versions = {item.id: item.version for item in self._items(self.appraisal).values()}
        version = self.appraisal.version

        scoring.score_appraisal(self.appraisal, self.user)

        self.assertEqual({item.id: item.version for item in self._items(self.appraisal).values()}, versions)
        self.assertEqual(Appraisal.objects.get(id=self.appraisal.id).version, version)


class ShortlistTest(ScoringTestCase):
    def _statuses(self, appraisals):
        return [(Appraisal.objects.get(id=a.id).status, Appraisal.objects.get(id=a.id).rank) for a in appraisals]

    def test_the_top_three_of_five_are_shortlisted_ties_by_submission_time(self):
        jobs = {0: "12", 1: "6", 2: "12", 3: "3", 4: "1"}
        appraisals = [self._eligible({"jobs_to_create": jobs[i]}, minutes=10 - i) for i in range(5)]
        scoring.score_round(self.user, "UMEP", "2026-R1")

        summary = scoring.shortlist_round(self.user, "UMEP", "2026-R1")

        self.assertEqual(summary, {None: {"places": 3, "ranked": 5, "shortlisted": 3}})
        self.assertEqual(self._statuses(appraisals), [("SHORTLISTED", 2), ("SHORTLISTED", 3), ("SHORTLISTED", 1),
                                                      ("NOT_SHORTLISTED", 4), ("NOT_SHORTLISTED", 5)])
        self.assertEqual(Individual.objects.get(id=appraisals[2].individual_id).json_ext["desk_appraisal_status"],
                         "SHORTLISTED")
        self.assertEqual(Individual.objects.get(id=appraisals[4].individual_id).json_ext["desk_appraisal_status"],
                         "NOT_SHORTLISTED")
        self.assertEqual(Appraisal.objects.get(id=appraisals[0].id).stage, "SHORTLISTING")

    def test_each_city_has_its_own_places(self):
        rules = {**RULES, "shortlist": {"per_city": {"Blantyre": 2, "Zomba": 3}}}
        blantyre, zomba, mzuzu = city_village("Blantyre"), city_village("Zomba"), city_village("Mzuzu")
        b = [self._eligible({"jobs_to_create": str(12 - i)}, rules=rules, location=blantyre, minutes=i)
             for i in range(3)]
        z = [self._eligible(rules=rules, location=zomba, minutes=i) for i in range(2)]
        m = [self._eligible(rules=rules, location=mzuzu)]

        summary = scoring.shortlist_round(self.user, "UMEP", "2026-R1")

        self.assertEqual(summary, {"Blantyre": {"places": 2, "ranked": 3, "shortlisted": 2},
                                   "Zomba": {"places": 3, "ranked": 2, "shortlisted": 2},
                                   "Mzuzu": {"places": 0, "ranked": 1, "shortlisted": 0}})
        self.assertEqual(self._statuses(b), [("SHORTLISTED", 1), ("SHORTLISTED", 2), ("NOT_SHORTLISTED", 3)])
        self.assertEqual(self._statuses(z), [("SHORTLISTED", 1), ("SHORTLISTED", 2)])
        self.assertEqual(self._statuses(m), [("NOT_SHORTLISTED", 1)])

    def test_eligible_appraisals_are_scored_first_and_a_rerun_recomputes(self):
        first = self._eligible({"jobs_to_create": "1"})
        scoring.shortlist_round(self.user, "UMEP", "2026-R1")
        self.assertEqual(self._statuses([first]), [("SHORTLISTED", 1)])

        later = [self._eligible() for _ in range(3)]
        scoring.shortlist_round(self.user, "UMEP", "2026-R1")

        self.assertEqual(self._statuses([first]), [("NOT_SHORTLISTED", 4)])
        self.assertEqual({status for status, _ in self._statuses(later)}, {"SHORTLISTED"})
        log = Appraisal.objects.get(id=first.id).json_ext["shortlist"]
        self.assertEqual((log["by"], log["groups"]), ("appraisalScorer",
                                                      [{"group": None, "places": 3, "ranked": 4, "shortlisted": 3}]))

    def test_changed_scores_or_nothing_to_rank_block_the_shortlist(self):
        with self.assertRaisesMessage(ValidationError, "appraisal.nothing_to_shortlist"):
            scoring.shortlist_round(self.user, "UMEP", "2026-R1")
        appraisal = scoring.score_appraisal(self._eligible(), self.user)
        AppraisalItem.objects.filter(appraisal=appraisal, key="stable_market").update(basis_changed=True)
        with self.assertRaisesMessage(ValidationError, "appraisal.scores_changed: 1 appraisals to review first"):
            scoring.shortlist_round(self.user, "UMEP", "2026-R1")


class ScoringConfigTest(TestCase):
    def test_score_rules_are_checked_with_enrolment(self):
        bad = {**RULES, "scoring": {"components": [
            {"key": "a", "label": "A", "field": "x", "weight": 5, "scoring": {"type": "numeric_bands", "bands": []}}]}}
        with self.assertRaises(ValidationError) as exc:
            validate_programmes({"UMEP": bad})
        self.assertIn("programmes.UMEP.scoring.components[0].scoring.bands must be a non-empty list.",
                      exc.exception.messages)
        validate_programmes({"UMEP": {**RULES, "shortlist": {"per_city": {"Zomba": 1}, "location_type": "R"}}})
        with self.assertRaises(ValidationError):
            validate_programmes({"UMEP": {**RULES, "shortlist": {"size": 1, "location_type": "X"}}})


class ScoringApiTest(openIMISGraphQLTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = create_test_interactive_user(username="appraisalScoreApi", roles=[create_admin_role().id])
        cls.token = BaseTestContext(user=cls.admin).get_jwt()

    def setUp(self):
        super().setUp()
        individual = create_individual(self.admin.username)
        self.appraisal = Appraisal(individual=individual, programme="UMEP", round="2026-R1", config_snapshot=RULES,
                                   status="ELIGIBLE", stage="DESK_SCORING", eligibility="ELIGIBLE",
                                   json_ext={"score_inputs": {**BEST, "market_stability": None}})
        self.appraisal.save(user=self.admin)

    def test_score_answer_and_shortlist_over_graphql(self):
        self.send_mutation_raw('mutation { scoreAppraisals(input: {programme: "UMEP", round: "2026-R1"}) '
                               '{ internalId } }', self.token)
        response = self.query(
            'query ($id: ID) { appraisal(id: $id) { edges { node { status score '
            'items(kind: "SCORE") { uuid key points maxPoints label } } } } }',
            variables={"id": str(self.appraisal.id)}, headers={"HTTP_AUTHORIZATION": f"Bearer {self.token}"})
        node = json.loads(response.content)["data"]["appraisal"]["edges"][0]["node"]
        self.assertEqual((node["status"], node["score"]), ("SCORED", "75.00"))
        market = next(item for item in node["items"] if item["key"] == "stable_market")
        self.assertEqual((market["maxPoints"], market["label"]), ("25", "Availability of stable market"))

        self.send_mutation_raw(f'mutation {{ answerAppraisalScore(input: {{id: "{market["uuid"]}", points: "25", '
                               f'evidence: "Customer records"}}) {{ internalId }} }}', self.token)
        self.send_mutation_raw('mutation { shortlistAppraisals(input: {programme: "UMEP", round: "2026-R1"}) '
                               '{ internalId } }', self.token)

        appraisal = Appraisal.objects.get(id=self.appraisal.id)
        self.assertEqual((appraisal.score, appraisal.status, appraisal.rank), (Decimal("100.00"), "SHORTLISTED", 1))
