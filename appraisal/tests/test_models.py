from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from appraisal.models import Appraisal, AppraisalItem
from core.test_helpers import create_admin_role, create_test_interactive_user
from individual.tests.test_helpers import create_individual, create_sp_role
from location.test_helpers import assign_user_districts, create_test_village

Kind, Result = AppraisalItem.Kind, AppraisalItem.Result


class AppraisalTestMixin:
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_interactive_user(username="appraisalAdmin", roles=[create_admin_role().id])
        cls.individual = create_individual(cls.user.username)

    def _appraisal(self, individual=None, **fields):
        appraisal = Appraisal(individual=individual or self.individual,
                              **{"programme": "UMEP", "round": "2026-R1", **fields})
        appraisal.save(user=self.user)
        return appraisal

    def _item(self, appraisal, **fields):
        item = AppraisalItem(appraisal=appraisal, **{"kind": Kind.ELIGIBILITY, "key": "youth_age", **fields})
        item.save(user=self.user)
        return item

    def assertRefused(self, create):
        with self.assertRaises(IntegrityError), transaction.atomic():
            create()


class AppraisalModelTest(AppraisalTestMixin, TestCase):
    def test_a_new_appraisal_starts_received_at_document_check(self):
        appraisal = self._appraisal()

        self.assertEqual((appraisal.stage, appraisal.status), (Appraisal.Stage.DOCUMENT_CHECK, Appraisal.Status.RECEIVED))
        self.assertIsNone(appraisal.eligibility)
        self.assertEqual((appraisal.user_created, appraisal.version), (self.user, 1))

    def test_every_change_is_kept_in_the_history_with_its_user(self):
        appraisal = self._appraisal()
        appraisal.status = Appraisal.Status.DESK_SCREENING
        appraisal.save(user=self.user)

        history = appraisal.history.order_by("history_date")
        self.assertEqual([h.status for h in history], ["RECEIVED", "DESK_SCREENING"])
        self.assertEqual(appraisal.version, 2)

    def test_one_active_appraisal_per_person_programme_and_round(self):
        first = self._appraisal()

        self.assertRefused(lambda: self._appraisal())
        self._appraisal(round="2027-R1")
        self._appraisal(programme="PWP")
        first.delete(user=self.user)
        self._appraisal()

    def test_score_and_rank_bounds(self):
        self.assertRefused(lambda: self._appraisal(score=Decimal("-1")))
        self.assertRefused(lambda: self._appraisal(rank=0))
        appraisal = self._appraisal(score=Decimal("87.50"), rank=1)
        self.assertEqual(appraisal.score, Decimal("87.50"))


class AppraisalItemModelTest(AppraisalTestMixin, TestCase):
    def setUp(self):
        self.appraisal = self._appraisal()

    def test_the_officer_value_counts_over_the_system_value(self):
        item = self._item(self.appraisal, system_result=Result.PENDING)
        self.assertEqual((item.result, item.is_overridden), (Result.PENDING, False))

        item.officer_result, item.decided_by, item.decided_at = Result.YES, self.user, timezone.now()
        item.save(user=self.user)
        self.assertEqual((item.result, item.is_overridden), (Result.YES, True))

        score = self._item(self.appraisal, kind=Kind.SCORE, key="turnover", system_points=Decimal("12"))
        self.assertEqual(score.points, Decimal("12"))
        score.officer_points, score.decided_by, score.decided_at = Decimal("0"), self.user, timezone.now()
        score.save(user=self.user)
        self.assertEqual(score.points, Decimal("0"))

    def test_an_item_holds_the_value_of_its_kind_only(self):
        self.assertRefused(lambda: self._item(self.appraisal, system_points=Decimal("5")))
        self.assertRefused(lambda: self._item(self.appraisal, kind=Kind.SCORE, system_result=Result.YES))
        with self.assertRaises(ValidationError):
            AppraisalItem(appraisal=self.appraisal, kind=Kind.SCORE, key="x", officer_result=Result.NO).clean()

    def test_an_officer_value_needs_who_decided_and_when(self):
        self.assertRefused(lambda: self._item(self.appraisal, officer_result=Result.NO))
        self.assertRefused(lambda: self._item(self.appraisal, officer_result=Result.NO, decided_by=self.user))

    def test_points_are_not_negative(self):
        self.assertRefused(lambda: self._item(self.appraisal, kind=Kind.SCORE, key="jobs", system_points=Decimal("-1")))

    def test_one_active_item_per_kind_and_key(self):
        first = self._item(self.appraisal)

        self.assertRefused(lambda: self._item(self.appraisal))
        self._item(self.appraisal, kind=Kind.SCORE, key="youth_age")
        first.delete(user=self.user)
        self._item(self.appraisal)


class AppraisalLocationScopeTest(AppraisalTestMixin, TestCase):
    """Appraisals follow the location rules of the individual appraised."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.village_a = create_test_village({"name": "Appraisal village A", "code": "ApVA"})
        cls.village_b = create_test_village({"name": "Appraisal village B", "code": "ApVB"})
        cls.person_a = create_individual(cls.user.username, {"location": cls.village_a})
        cls.person_b = create_individual(cls.user.username, {"location": cls.village_b})
        cls.officer_a = create_test_interactive_user(username="appraisalOfficerA",
                                                     roles=[create_sp_role(cls.user).id])
        assign_user_districts(cls.officer_a, [cls.village_a.parent.parent.code])

    def setUp(self):
        self.appraisal_a = self._appraisal(individual=self.person_a)
        self.appraisal_b = self._appraisal(individual=self.person_b)
        self.item_a = self._item(self.appraisal_a)
        self.item_b = self._item(self.appraisal_b)

    def test_an_officer_sees_only_appraisals_in_their_districts(self):
        self.assertEqual(set(Appraisal.get_queryset(None, self.officer_a)), {self.appraisal_a})
        self.assertEqual(set(AppraisalItem.get_queryset(None, self.officer_a)), {self.item_a})

    def test_an_administrator_sees_all(self):
        appraisals = set(Appraisal.get_queryset(None, self.user))
        self.assertTrue({self.appraisal_a, self.appraisal_b} <= appraisals)

    def test_a_given_queryset_is_narrowed_not_replaced(self):
        mine = Appraisal.objects.filter(id__in=[self.appraisal_a.id, self.appraisal_b.id], programme="UMEP")
        self.assertEqual(list(Appraisal.get_queryset(mine, self.officer_a)), [self.appraisal_a])
