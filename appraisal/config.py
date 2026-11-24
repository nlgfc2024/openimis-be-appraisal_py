"""Shape of the appraisal module configuration. Every problem is reported with its path, e.g.
``programmes.UMEP.eligibility[2].key``. Strategy and check names must exist; score rules are checked for
shape (their points by the scoring)."""
import re

from django.core.exceptions import ValidationError

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
PERMS_SUFFIX = "_perms"
PROGRAMME_KEYS = {"description", "source_types", "round", "eligibility", "inputs", "scoring", "shortlist"}
ELIGIBILITY_KEYS = {"key", "label", "field", "strategy", "check", "params", "location_type"}
LOCATION_TYPES = ("R", "D", "W", "V")
SCORING_KEYS = {"normalise_to", "components"}
COMPONENT_KEYS = {"key", "label", "field", "weight", "scoring"}
SHORTLIST_KEYS = {"size", "per_city"}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_text(value):
    return isinstance(value, str) and bool(value.strip())


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _unexpected(errors, value, allowed, path):
    extra = sorted(set(value) - allowed)
    if extra:
        errors.append(f"{path} has unsupported keys: {', '.join(extra)}.")


def _keyed_items(errors, items, path, check):
    """A list of objects, each with a unique ``key``, checked by ``check(item, item_path)``."""
    if not isinstance(items, list):
        errors.append(f"{path} must be a list.")
        return
    seen = set()
    for index, item in enumerate(items):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{item_path} must be an object.")
            continue
        key = item.get("key")
        if not isinstance(key, str) or not KEY_PATTERN.match(key):
            errors.append(f"{item_path}.key must be lower case letters, digits and _, starting with a letter.")
        elif key in seen:
            errors.append(f"{item_path}.key '{key}' is used twice.")
        else:
            seen.add(key)
        if not _is_text(item.get("label")):
            errors.append(f"{item_path}.label must be a non-empty string.")
        check(errors, item, item_path)


def _eligibility_item(errors, item, path):
    from calcrule_validations.strategies import ValidationStrategyStorage

    from appraisal.checks import CHECKS

    _unexpected(errors, item, ELIGIBILITY_KEYS, path)
    strategy, check = item.get("strategy"), item.get("check")
    if strategy is not None and not _is_text(strategy):
        errors.append(f"{path}.strategy must be a strategy name, or null for an item only an officer judges.")
    elif strategy and ValidationStrategyStorage.choose_strategy(strategy) is None:
        errors.append(f"{path}.strategy '{strategy}' is not a calcrule_validations strategy.")
    if check is not None and check not in CHECKS:
        errors.append(f"{path}.check must be one of: {', '.join(sorted(CHECKS))}.")
    if strategy and check:
        errors.append(f"{path} has both a strategy and a check; use one.")
    if "location_type" in item and item["location_type"] not in LOCATION_TYPES:
        errors.append(f"{path}.location_type must be one of: {', '.join(LOCATION_TYPES)}.")
    if "field" in item and not _is_text(item["field"]):
        errors.append(f"{path}.field must be a non-empty string.")
    if strategy is not None and "field" not in item:
        errors.append(f"{path}.field is required when a strategy pre-fills the item.")
    if "params" in item and not isinstance(item["params"], dict):
        errors.append(f"{path}.params must be an object.")


def _inputs(errors, inputs, path):
    from appraisal.prefill import DERIVATIONS

    if not isinstance(inputs, dict):
        errors.append(f"{path} must be an object of input name: how it is derived.")
        return
    for name, spec in inputs.items():
        spec_path = f"{path}.{name}"
        if not isinstance(spec, dict) or spec.get("derive") not in DERIVATIONS:
            errors.append(f"{spec_path}.derive must be one of: {', '.join(sorted(DERIVATIONS))}.")
            continue
        needed = DERIVATIONS[spec["derive"]][1]
        _unexpected(errors, spec, {"derive", *needed}, spec_path)
        for key in needed:
            value = spec.get(key)
            text = _is_text(value) if key == "field" else (
                isinstance(value, list) and bool(value) and all(_is_text(v) for v in value))
            if not text:
                expected = "a question name" if key == "field" else "a non-empty list of names"
                errors.append(f"{spec_path}.{key} must be {expected}.")


def _score_component(errors, item, path):
    _unexpected(errors, item, COMPONENT_KEYS, path)
    if not _is_text(item.get("field")):
        errors.append(f"{path}.field must be a non-empty string.")
    weight = item.get("weight")
    if not _is_number(weight) or weight <= 0:
        errors.append(f"{path}.weight must be a positive number.")
    if not isinstance(item.get("scoring"), dict):
        errors.append(f"{path}.scoring must be an object.")


def _scoring(errors, scoring, path):
    if not isinstance(scoring, dict):
        errors.append(f"{path} must be an object.")
        return
    _unexpected(errors, scoring, SCORING_KEYS, path)
    normalise_to = scoring.get("normalise_to", 100)
    if not _is_number(normalise_to) or normalise_to <= 0:
        errors.append(f"{path}.normalise_to must be a positive number.")
    components = scoring.get("components")
    if not isinstance(components, list) or not components:
        errors.append(f"{path}.components must be a non-empty list.")
        return
    _keyed_items(errors, components, f"{path}.components", _score_component)


def _shortlist(errors, shortlist, path):
    if not isinstance(shortlist, dict):
        errors.append(f"{path} must be an object.")
        return
    _unexpected(errors, shortlist, SHORTLIST_KEYS, path)
    if "size" not in shortlist and "per_city" not in shortlist:
        errors.append(f"{path} needs size, per_city, or both.")
    if "size" in shortlist and not _positive_int(shortlist["size"]):
        errors.append(f"{path}.size must be a positive whole number.")
    if "per_city" in shortlist:
        per_city = shortlist["per_city"]
        if not isinstance(per_city, dict) or not per_city:
            errors.append(f"{path}.per_city must be a non-empty object of city: places.")
        else:
            for city, places in per_city.items():
                if not _is_text(city) or not _positive_int(places):
                    errors.append(f"{path}.per_city['{city}'] must be a positive whole number of places.")


def _programme(errors, code, programme, path):
    if not isinstance(programme, dict):
        errors.append(f"{path} must be an object.")
        return
    _unexpected(errors, programme, PROGRAMME_KEYS, path)
    sources = programme.get("source_types")
    if not isinstance(sources, list) or not sources or not all(_is_text(source) for source in sources):
        errors.append(f"{path}.source_types must be a non-empty list of source keys.")
    if not _is_text(programme.get("round")):
        errors.append(f"{path}.round must be a non-empty string.")
    if "description" in programme and not isinstance(programme["description"], str):
        errors.append(f"{path}.description must be a string.")
    _keyed_items(errors, programme.get("eligibility", []), f"{path}.eligibility", _eligibility_item)
    if "inputs" in programme:
        _inputs(errors, programme["inputs"], f"{path}.inputs")
    if "scoring" in programme:
        _scoring(errors, programme["scoring"], f"{path}.scoring")
    if "shortlist" in programme:
        _shortlist(errors, programme["shortlist"], f"{path}.shortlist")


def validate_programmes(programmes, path="programmes"):
    """Raise ValidationError listing every problem in ``programmes``."""
    errors = []
    if not isinstance(programmes, dict):
        raise ValidationError(f"{path} must be an object keyed by programme code.")
    for code, programme in programmes.items():
        if not _is_text(code):
            errors.append(f"{path} has an empty programme code.")
            continue
        _programme(errors, code, programme, f"{path}.{code}")
    if errors:
        raise ValidationError(errors)


def validate_config(config):
    """Raise ValidationError for a module configuration whose rights or programmes are malformed."""
    errors = []
    for name, value in config.items():
        if name.endswith(PERMS_SUFFIX) and (
                not isinstance(value, list) or not all(isinstance(code, str) and code.isdigit() for code in value)):
            errors.append(f"{name} must be a list of right codes as strings.")
    try:
        validate_programmes(config.get("programmes", {}))
    except ValidationError as exc:
        errors.extend(exc.messages)
    if errors:
        raise ValidationError(errors)
