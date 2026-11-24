# openIMIS Backend Appraisal Module

`openimis-be-appraisal` runs the desk appraisal of programme applications: the checks, scoring and shortlist
between an application's import and physical verification. It is programme-neutral; each programme's
checklist, score rules and shortlist come from configuration. Urban MEP (UMEP) is the first programme, with
applications imported from Kobo by `msr_etl` (Urban MEP Operations Manual, chapter 8 and Annex 2A/2B).

Built in work packages tracked in [#1](https://github.com/nlgfc2024/openimis-be-appraisal_py/issues/1). This
version holds the module skeleton: configuration, its validation and the rights.

## Installation

Place this repository next to `openimis-be_py` and add it to `openimis.json`:

```json
{
  "name": "appraisal",
  "pip": "-e ../openimis-be-appraisal_py"
}
```

### Rollout order

The module reads other modules' new code at startup (msr_etl's signals, calcrule_validations' strategies,
enrolment's scoring helpers), so a partial rollout won't boot. Deploy in this order:

1. openimis-be-msr_etl_py#129: the Kobo submission and document signals.
2. openimis-be-calcrule_validations_py#1, with openimis-be_py#21 pinning the hub to the nlgfc2024 fork.
3. openimis-be-enrolment_py#14: the public scoring and quota helpers.
4. openimis-be-household_validation_py#48: `requires_json_ext`, which selects shortlisted applicants.
5. This module and openimis-fe-appraisal_js, with their `openimis.json` entries in the backend and frontend hubs.
6. The programme's configuration (for UMEP, nlgfc2024/coremis-configs#11), applied last.

## Configuration

`ModuleConfiguration` for module `appraisal` (layer `be`). Saved top-level keys replace the defaults. The
configuration is checked when it is saved and again at start; every problem is reported with its path, e.g.
`programmes.UMEP.eligibility[2].key`, and an invalid configuration stops the module from starting.

```json
{
  "programmes": {
    "UMEP": {
      "source_types": ["kobo"],
      "round": "2026-R1",
      "eligibility": [
        {"key": "youth_age", "label": "Youth aged 18-35", "field": "dob",
         "strategy": "AgeRangeValidationStrategy", "params": {"min": 18, "max": 35}},
        {"key": "co_financing", "label": "Minimum 30% co-financing", "strategy": null}
      ],
      "scoring": {
        "normalise_to": 100,
        "components": [
          {"key": "turnover", "label": "Annual turnover", "field": "turnover_past_12m_mwk", "weight": 15,
           "scoring": {"type": "numeric_bands", "bands": [{"min": 5000001, "points": 15}, {"min": 3000000, "points": 12}]}}
        ]
      },
      "shortlist": {"size": 1200, "per_city": {"Blantyre": 526, "Zomba": 104, "Lilongwe": 401, "Mzuzu": 169}}
    }
  }
}
```

| Key | Meaning |
| --- | --- |
| `source_types` | msr_etl sources whose applications this programme appraises. |
| `round` | The intake round; one appraisal per person, programme and round. |
| `eligibility` | The prerequisite checklist. Each item has a unique `key` and a `label`; `strategy` names the calcrule_validations strategy that pre-fills it from `field` (with `params`), or `null` for an item only an officer judges. |
| `scoring` | Score components: `key`, `label`, `field`, `weight` and `scoring` in enrolment's weighted-score shape (`mapping` or `numeric_bands`); the total is normalised to `normalise_to` (default 100). |
| `shortlist` | Places: `size` overall, and/or `per_city` places by city. |

Strategy names and score rules are checked in full by the pre-fill and scoring work packages; this version
checks their shape.

## Rights

| Right | Code | Config key |
| --- | --- | --- |
| View appraisals | 220001 | `gql_query_appraisal_perms` |
| Screen (record checks, decide eligibility) | 220002 | `gql_mutation_screen_appraisal_perms` |
| Score | 220003 | `gql_mutation_score_appraisal_perms` |
| Shortlist | 220004 | `gql_mutation_shortlist_appraisal_perms` |
| Review documents | 220005 | `gql_mutation_review_document_perms` |
| Return for action | 220006 | `gql_mutation_return_appraisal_perms` |
| Approve | 220007 | `gql_mutation_approve_appraisal_perms` |

The 220000 block is reserved for this module; a test fails if another installed module defaults to one of
these codes. Rights are given to roles in the usual openIMIS role administration.

## Testing

From `openimis-be_py/openIMIS`: `python manage.py test appraisal --keepdb`.
