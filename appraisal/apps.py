import copy
import json
import logging
import os
from datetime import datetime

from django.apps import AppConfig
from django.core.exceptions import ValidationError

from core.module_config_registry import register_validator

logger = logging.getLogger(__name__)

MODULE_NAME = "appraisal"

# One right per role action of the desk appraisal (Urban MEP Operations Manual 8.9). The 220000 block is
# reserved for this module; tests fail if another installed module defaults to one of these codes.
RIGHT_APPRAISAL_VIEW = 220001
RIGHT_APPRAISAL_SCREEN = 220002
RIGHT_APPRAISAL_SCORE = 220003
RIGHT_APPRAISAL_SHORTLIST = 220004
RIGHT_APPRAISAL_REVIEW_DOCUMENTS = 220005
RIGHT_APPRAISAL_RETURN = 220006
RIGHT_APPRAISAL_APPROVE = 220007

APPRAISAL_RIGHTS = (
    RIGHT_APPRAISAL_VIEW,
    RIGHT_APPRAISAL_SCREEN,
    RIGHT_APPRAISAL_SCORE,
    RIGHT_APPRAISAL_SHORTLIST,
    RIGHT_APPRAISAL_REVIEW_DOCUMENTS,
    RIGHT_APPRAISAL_RETURN,
    RIGHT_APPRAISAL_APPROVE,
)

DEFAULT_CONFIG = {
    "gql_query_appraisal_perms": [str(RIGHT_APPRAISAL_VIEW)],
    "gql_mutation_screen_appraisal_perms": [str(RIGHT_APPRAISAL_SCREEN)],
    "gql_mutation_score_appraisal_perms": [str(RIGHT_APPRAISAL_SCORE)],
    "gql_mutation_shortlist_appraisal_perms": [str(RIGHT_APPRAISAL_SHORTLIST)],
    "gql_mutation_review_document_perms": [str(RIGHT_APPRAISAL_REVIEW_DOCUMENTS)],
    "gql_mutation_return_appraisal_perms": [str(RIGHT_APPRAISAL_RETURN)],
    "gql_mutation_approve_appraisal_perms": [str(RIGHT_APPRAISAL_APPROVE)],
    # Programmes appraised, keyed by programme code; none by default. See README "Configuration" and
    # appraisal.config.validate_programmes for the shape.
    "programmes": {},
}


class AppraisalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = MODULE_NAME

    gql_query_appraisal_perms = None
    gql_mutation_screen_appraisal_perms = None
    gql_mutation_score_appraisal_perms = None
    gql_mutation_shortlist_appraisal_perms = None
    gql_mutation_review_document_perms = None
    gql_mutation_return_appraisal_perms = None
    gql_mutation_approve_appraisal_perms = None
    programmes = None
    # Why the saved configuration couldn't be loaded; None when it loaded
    config_error = None

    @classmethod
    def _load_config(cls, cfg):
        from appraisal.config import validate_config

        validate_config(cfg)
        for field in cfg:
            if hasattr(cls, field):
                setattr(cls, field, cfg[field])

    def ready(self):
        register_validator(self.name, self._validate_module_config)
        try:
            saved = _saved_config(self.name)
            self._load_config({**copy.deepcopy(DEFAULT_CONFIG), **(saved or {})})
            AppraisalConfig.config_error = None
        except ValidationError as exc:
            # A broken saved row disables this module's programmes, not the backend; defaults are never used silently
            AppraisalConfig.config_error = "; ".join(exc.messages)
            self._load_config(copy.deepcopy(DEFAULT_CONFIG))
            logger.error("The saved %s configuration is invalid, so no programme is appraised until it is fixed: %s",
                         self.name, AppraisalConfig.config_error)
        from appraisal import receivers

        receivers.connect()

    def _validate_module_config(self, instance):
        """Reject an invalid configuration on save, before the next restart reads it."""
        from appraisal.config import validate_config

        saved = instance._cfg
        if not isinstance(saved, dict):
            raise ValidationError({"config": "Module configuration must be an object."})
        try:
            validate_config({**DEFAULT_CONFIG, **saved})
        except ValidationError as exc:
            raise ValidationError({"config": exc.messages}) from exc

    @classmethod
    def check_configured(cls):
        """Refuse to work while the saved configuration is invalid (see ready)."""
        if cls.config_error:
            raise ValidationError(f"appraisal.not_configured: {cls.config_error}")

    @classmethod
    def programme(cls, code):
        """The configuration of programme ``code``, or None when it isn't appraised."""
        return (cls.programmes or {}).get(code)


def _saved_config(module):
    """The saved configuration of ``module``; None when there is none, or no database yet. Unlike
    ModuleConfiguration.get_or_default, a saved row that isn't a JSON object raises instead of quietly falling back
    to the defaults, so ready() can report it."""
    from django.db import DatabaseError
    from django.db.models import Q

    from core.models import ModuleConfiguration

    if os.environ.get("NO_DATABASE"):
        return None
    try:
        row = ModuleConfiguration.objects.filter(
            Q(is_disabled_until=None) | Q(is_disabled_until__lt=datetime.now()), layer="be", module=module).first()
    except DatabaseError:
        # Core's tables don't exist yet (first migrate), as get_or_default allows
        return None
    if row is None:
        return None
    try:
        saved = json.loads(row.config)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"The saved {module} configuration is not valid JSON: {exc}") from exc
    if not isinstance(saved, dict):
        raise ValidationError(f"The saved {module} configuration must be a JSON object.")
    return saved
