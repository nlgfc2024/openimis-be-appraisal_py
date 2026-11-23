# openIMIS Backend Appraisal Module

`openimis-be-appraisal` runs the desk appraisal of programme applications: the checks, scoring and shortlist
between an application's import and physical verification. It is programme-neutral; each programme's
checklist, score rules and shortlist come from configuration. Urban MEP (UMEP) is the first programme, with
applications imported from Kobo by `msr_etl` (Urban MEP Operations Manual, chapter 8 and Annex 2A/2B).

Built in work packages tracked in [#1](https://github.com/nlgfc2024/openimis-be-appraisal_py/issues/1). This
version holds the configuration, its validation, the rights and the data model.

## Installation

Place this repository next to `openimis-be_py` and add it to `openimis.json`:

```json
{
  "name": "appraisal",
  "pip": "-e ../openimis-be-appraisal_py"
}
```

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

## Data model

Both models keep every change in a history table (`simple_history`) with the user and time (Operations
Manual 8.11), and are soft-deleted (`is_deleted`).

| Model | Holds |
| --- | --- |
| `Appraisal` (`HistoryBusinessModel`) | One per person, `programme` and `round` (unique among active appraisals): the `submission` appraised (msr_etl `KoboSubmission`), `stage`, `status`, `eligibility`, `score`, `rank`, and `config_snapshot`, the programme's rules when it was created. |
| `AppraisalItem` (`HistoryModel`) | One per checklist item (`ELIGIBILITY`, a result: `YES` / `NO` / `PENDING`) or score component (`SCORE`, points), unique per appraisal, kind and `key`. The system's value with its `basis`, and the officer's value with `evidence`, `comment`, `decided_by` and `decided_at`. |

- **Stage and status.** `stage` is where the application is: `DOCUMENT_CHECK`, `DESK_SCREENING`, `DESK_SCORING`, `SHORTLISTING`, `CLOSED`. `status` is its outcome so far: `RECEIVED`, `DOCUMENT_CHECK`, `DESK_SCREENING`, `ELIGIBLE`, `INELIGIBLE`, `SCORED`, `SHORTLISTED`, `NOT_SHORTLISTED`, `RETURNED` (8.13). A returned application keeps the stage it was returned from.
- **Which value counts.** `AppraisalItem.result` and `.points` give the officer's value when one is recorded, else the system's; `is_overridden` says which. `basis_changed` flags that the answers changed after an officer recorded a value.
- **Enforced in the database.** An item holds only the value of its kind; points and scores are never negative; ranks start at 1; an officer's value always has `decided_by` and `decided_at`.
- **Access.** `Appraisal.get_queryset(queryset, user)` and `AppraisalItem.get_queryset` follow `Individual.get_queryset`: a user sees the appraisals of the individuals in their locations.

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
