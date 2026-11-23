from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from core import models as core_models
from individual.models import Individual

POINTS = {"max_digits": 7, "decimal_places": 2}


class Appraisal(core_models.HistoryBusinessModel):
    """One person's desk appraisal for one programme and intake round (Urban MEP Operations Manual, 8.2-8.13).

    ``stage`` is where the application is in the workflow; ``status`` is its outcome so far. A returned
    application keeps the stage it was returned from. Every change is kept in the history table.
    """
    USE_CACHE = False

    class Stage(models.TextChoices):
        DOCUMENT_CHECK = "DOCUMENT_CHECK", _("Document check")
        DESK_SCREENING = "DESK_SCREENING", _("Desk screening")
        DESK_SCORING = "DESK_SCORING", _("Desk scoring")
        SHORTLISTING = "SHORTLISTING", _("Shortlisting")
        CLOSED = "CLOSED", _("Closed")

    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", _("Application received")
        DOCUMENT_CHECK = "DOCUMENT_CHECK", _("Document check")
        DESK_SCREENING = "DESK_SCREENING", _("Desk screening")
        ELIGIBLE = "ELIGIBLE", _("Eligible")
        INELIGIBLE = "INELIGIBLE", _("Ineligible")
        SCORED = "SCORED", _("Scored")
        SHORTLISTED = "SHORTLISTED", _("Shortlisted")
        NOT_SHORTLISTED = "NOT_SHORTLISTED", _("Not shortlisted")
        RETURNED = "RETURNED", _("Returned for action")

    class Eligibility(models.TextChoices):
        ELIGIBLE = "ELIGIBLE", _("Eligible")
        INELIGIBLE = "INELIGIBLE", _("Ineligible")
        CLARIFICATION = "CLARIFICATION", _("Clarification required")

    individual = models.ForeignKey(Individual, models.DO_NOTHING, related_name="appraisals")
    # The application appraised; kept even if it's later superseded, so the appraisal stays auditable
    submission = models.ForeignKey("msr_etl.KoboSubmission", models.PROTECT, null=True, blank=True,
                                   related_name="appraisals")
    programme = models.CharField(max_length=64)
    round = models.CharField(max_length=64)
    stage = models.CharField(max_length=32, choices=Stage.choices, default=Stage.DOCUMENT_CHECK)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.RECEIVED)
    eligibility = models.CharField(max_length=32, choices=Eligibility.choices, null=True, blank=True)
    score = models.DecimalField(**POINTS, null=True, blank=True)
    rank = models.PositiveIntegerField(null=True, blank=True)
    # The programme's rules when the appraisal was created, so a later config change can't alter its basis
    config_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["individual", "programme", "round"],
                condition=Q(is_deleted=False),
                name="appraisal_one_active_per_person_programme_round",
            ),
            models.CheckConstraint(check=Q(score__isnull=True) | Q(score__gte=0), name="appraisal_score_not_negative"),
            models.CheckConstraint(check=Q(rank__isnull=True) | Q(rank__gte=1), name="appraisal_rank_from_one"),
        ]
        indexes = [
            models.Index(fields=["programme", "round", "status"], name="appraisal_programme_status"),
            models.Index(fields=["programme", "round", "score"], name="appraisal_programme_score"),
        ]

    def __str__(self):
        return f"{self.programme} {self.round}: {self.individual_id} ({self.status})"

    @classmethod
    def get_queryset(cls, queryset, user):
        """Appraisals of the individuals ``user`` may see (Individual.get_queryset's location rules)."""
        if queryset is None:
            queryset = cls.objects.all()
        return queryset.filter(individual__in=Individual.get_queryset(None, user))


class AppraisalItem(core_models.HistoryModel):
    """One checklist item (ELIGIBILITY: Yes / No / Pending) or score component (SCORE: points) of an appraisal.

    The system's value comes from the application's answers; the officer's, when given, decides. ``basis``
    holds what the system value was worked out from, and ``basis_changed`` flags that the answers changed
    after an officer recorded a value.
    """
    USE_CACHE = False

    class Kind(models.TextChoices):
        ELIGIBILITY = "ELIGIBILITY", _("Eligibility")
        SCORE = "SCORE", _("Score")

    class Result(models.TextChoices):
        YES = "YES", _("Yes")
        NO = "NO", _("No")
        PENDING = "PENDING", _("Pending")

    appraisal = models.ForeignKey(Appraisal, models.DO_NOTHING, related_name="items")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    key = models.CharField(max_length=64)
    system_result = models.CharField(max_length=16, choices=Result.choices, null=True, blank=True)
    system_points = models.DecimalField(**POINTS, null=True, blank=True)
    basis = models.JSONField(default=dict, blank=True)
    basis_changed = models.BooleanField(default=False)
    officer_result = models.CharField(max_length=16, choices=Result.choices, null=True, blank=True)
    officer_points = models.DecimalField(**POINTS, null=True, blank=True)
    evidence = models.TextField(blank=True, default="")
    comment = models.TextField(blank=True, default="")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, models.DO_NOTHING, null=True, blank=True,
                                   related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["appraisal", "kind", "key"],
                condition=Q(is_deleted=False),
                name="appraisal_item_one_active_per_key",
            ),
            # A checklist item holds results, a score component points; never the other
            models.CheckConstraint(
                check=(Q(kind="ELIGIBILITY", system_points__isnull=True, officer_points__isnull=True)
                       | Q(kind="SCORE", system_result__isnull=True, officer_result__isnull=True)),
                name="appraisal_item_value_matches_kind",
            ),
            models.CheckConstraint(
                check=(Q(system_points__isnull=True) | Q(system_points__gte=0))
                & (Q(officer_points__isnull=True) | Q(officer_points__gte=0)),
                name="appraisal_item_points_not_negative",
            ),
            # An officer's value is always recorded with who decided it and when (8.11)
            models.CheckConstraint(
                check=(Q(officer_result__isnull=True, officer_points__isnull=True)
                       | Q(decided_by__isnull=False, decided_at__isnull=False)),
                name="appraisal_item_officer_value_is_attributed",
            ),
        ]

    def __str__(self):
        return f"{self.kind} {self.key}: {self.result if self.kind == self.Kind.ELIGIBILITY else self.points}"

    def clean(self):
        super().clean()
        if self.kind == self.Kind.ELIGIBILITY and (self.system_points is not None or self.officer_points is not None):
            raise ValidationError(_("An eligibility item holds a result, not points."))
        if self.kind == self.Kind.SCORE and (self.system_result or self.officer_result):
            raise ValidationError(_("A score item holds points, not a result."))

    @property
    def is_overridden(self):
        """Whether an officer's value replaces the system's."""
        return self.officer_result is not None or self.officer_points is not None

    @property
    def result(self):
        """The result that counts: the officer's when given, else the system's."""
        return self.officer_result or self.system_result

    @property
    def points(self):
        """The points that count: the officer's when given, else the system's."""
        return self.officer_points if self.officer_points is not None else self.system_points

    @classmethod
    def get_queryset(cls, queryset, user):
        if queryset is None:
            queryset = cls.objects.all()
        return queryset.filter(appraisal__in=Appraisal.get_queryset(None, user))
