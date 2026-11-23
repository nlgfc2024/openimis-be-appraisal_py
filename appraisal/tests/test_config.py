import copy
import importlib
import json
from unittest.mock import patch

from django.apps import apps
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from appraisal.apps import APPRAISAL_RIGHTS, DEFAULT_CONFIG, AppraisalConfig
from appraisal.config import validate_config, validate_programmes
from core.models import ModuleConfiguration

PROGRAMME = {
    "description": "Urban MEP desk appraisal",
    "source_types": ["kobo"],
    "round": "2026-R1",
    "eligibility": [
        {"key": "youth_age", "label": "Youth aged 18-35", "field": "dob",
         "strategy": "AgeRangeValidationStrategy", "params": {"min": 18, "max": 35}},
        {"key": "co_financing", "label": "Minimum 30% co-financing", "strategy": None},
    ],
    "scoring": {
        "normalise_to": 100,
        "components": [
            {"key": "turnover", "label": "Annual turnover", "field": "turnover_past_12m_mwk", "weight": 15,
             "scoring": {"type": "numeric_bands", "bands": [{"min": 5000001, "points": 15}]}},
        ],
    },
    "shortlist": {"size": 1200, "per_city": {"Blantyre": 526, "Zomba": 104, "Lilongwe": 401, "Mzuzu": 169}},
}


def _with(path, value):
    """PROGRAMME with the value at ``path`` (keys and list indexes) replaced, or removed when value is ...."""
    programme = copy.deepcopy(PROGRAMME)
    target = programme
    for step in path[:-1]:
        target = target[step]
    if value is ...:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return {"UMEP": programme}


class ProgrammeConfigTest(SimpleTestCase):
    def assertRejected(self, programmes, message):
        with self.assertRaises(ValidationError) as exc:
            validate_programmes(programmes)
        self.assertIn(message, exc.exception.messages)

    def test_default_and_complete_configs_are_valid(self):
        validate_config(DEFAULT_CONFIG)
        validate_programmes({"UMEP": PROGRAMME})
        validate_programmes({"UMEP": {"source_types": ["kobo"], "round": "R1"}})

    def test_programmes_must_be_an_object(self):
        self.assertRejected([], "programmes must be an object keyed by programme code.")

    def test_programme_requirements(self):
        cases = [
            (_with(["source_types"], []), "programmes.UMEP.source_types must be a non-empty list of source keys."),
            (_with(["round"], ...), "programmes.UMEP.round must be a non-empty string."),
            (_with(["colour"], "red"), "programmes.UMEP has unsupported keys: colour."),
        ]
        for programmes, message in cases:
            with self.subTest(message=message):
                self.assertRejected(programmes, message)

    def test_eligibility_items(self):
        duplicate = _with(["round"], "R1")
        duplicate["UMEP"]["eligibility"].append({"key": "youth_age", "label": "Again", "strategy": None})
        cases = [
            (_with(["eligibility", 0, "key"], "Youth Age"),
             "programmes.UMEP.eligibility[0].key must be lower case letters, digits and _, starting with a letter."),
            (duplicate, "programmes.UMEP.eligibility[2].key 'youth_age' is used twice."),
            (_with(["eligibility", 0, "field"], ...),
             "programmes.UMEP.eligibility[0].field is required when a strategy pre-fills the item."),
            (_with(["eligibility", 0, "params"], [18, 35]), "programmes.UMEP.eligibility[0].params must be an object."),
            (_with(["eligibility", 1, "label"], " "), "programmes.UMEP.eligibility[1].label must be a non-empty string."),
        ]
        for programmes, message in cases:
            with self.subTest(message=message):
                self.assertRejected(programmes, message)

    def test_scoring_and_shortlist(self):
        cases = [
            (_with(["scoring", "components"], []), "programmes.UMEP.scoring.components must be a non-empty list."),
            (_with(["scoring", "components", 0, "weight"], 0),
             "programmes.UMEP.scoring.components[0].weight must be a positive number."),
            (_with(["shortlist"], {}), "programmes.UMEP.shortlist needs size, per_city, or both."),
            (_with(["shortlist", "per_city", "Zomba"], 0),
             "programmes.UMEP.shortlist.per_city['Zomba'] must be a positive whole number of places."),
            (_with(["shortlist", "size"], True), "programmes.UMEP.shortlist.size must be a positive whole number."),
        ]
        for programmes, message in cases:
            with self.subTest(message=message):
                self.assertRejected(programmes, message)

    def test_every_problem_is_reported(self):
        programmes = _with(["round"], "")
        programmes["UMEP"]["source_types"] = "kobo"
        with self.assertRaises(ValidationError) as exc:
            validate_programmes(programmes)
        self.assertEqual(len(exc.exception.messages), 2)

    def test_rights_must_be_lists_of_codes(self):
        with self.assertRaisesMessage(ValidationError, "gql_query_appraisal_perms must be a list of right codes"):
            validate_config({**DEFAULT_CONFIG, "gql_query_appraisal_perms": 220001})


class AppraisalConfigLoadTest(SimpleTestCase):
    def setUp(self):
        saved = {name: getattr(AppraisalConfig, name) for name in DEFAULT_CONFIG}
        self.addCleanup(lambda: [setattr(AppraisalConfig, name, value) for name, value in saved.items()])

    def test_loads_programmes_and_rights(self):
        AppraisalConfig._load_config({**DEFAULT_CONFIG, "programmes": {"UMEP": PROGRAMME}})

        self.assertEqual(AppraisalConfig.programme("UMEP")["round"], "2026-R1")
        self.assertIsNone(AppraisalConfig.programme("PWP"))
        self.assertEqual(AppraisalConfig.gql_query_appraisal_perms, ["220001"])

    def test_an_invalid_configuration_fails_at_start(self):
        with self.assertRaises(ValidationError):
            AppraisalConfig._load_config({**DEFAULT_CONFIG, "programmes": {"UMEP": {}}})


class ModuleConfigurationSaveTest(TestCase):
    def test_invalid_configuration_is_rejected_on_save(self):
        instance = ModuleConfiguration(module="appraisal", layer="be", version="test",
                                       config=json.dumps({"programmes": {"UMEP": {"round": "R1"}}}))
        with self.assertRaises(ValidationError) as exc:
            instance.save()
        self.assertIn("programmes.UMEP.source_types must be a non-empty list of source keys.",
                      exc.exception.message_dict["config"])
        self.assertFalse(ModuleConfiguration.objects.filter(module="appraisal").exists())

    def test_valid_configuration_is_saved(self):
        with patch("core.models.base.transaction.on_commit"):
            ModuleConfiguration(module="appraisal", layer="be", version="test",
                                config=json.dumps({"programmes": {"UMEP": PROGRAMME}})).save()
        self.assertTrue(ModuleConfiguration.objects.filter(module="appraisal").exists())


class RightCodesTest(SimpleTestCase):
    def test_no_other_module_defaults_to_an_appraisal_right(self):
        """The 220000 block is reserved for appraisal; another module's default rights must not reuse it."""
        clashes = []
        for app in apps.get_app_configs():
            if app.name == "appraisal":
                continue
            try:
                module = importlib.import_module(f"{app.name}.apps")
            except ImportError:
                continue
            for config_name in ("DEFAULT_CONFIG", "DEFAULT_CFG"):
                config = getattr(module, config_name, None)
                if not isinstance(config, dict):
                    continue
                for key, value in config.items():
                    codes = value if isinstance(value, (list, tuple)) else [value]
                    clashes += [f"{app.name}.{key}={code}" for code in codes
                                if str(code).isdigit() and int(code) in APPRAISAL_RIGHTS]
        self.assertEqual(clashes, [])

    def test_rights_are_the_reserved_block(self):
        self.assertEqual(list(APPRAISAL_RIGHTS), list(range(220001, 220008)))
