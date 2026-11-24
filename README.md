# openIMIS Backend Appraisal Module

`openimis-be-appraisal` runs the desk appraisal of programme applications: the checks, scoring and shortlist
between an application's import and physical verification. It is programme-neutral; each programme's
checklist, score rules and shortlist come from configuration. Urban MEP (UMEP) is the first programme, with
applications imported from Kobo by `msr_etl` (Urban MEP Operations Manual, chapter 8 and Annex 2A/2B).

Built in work packages tracked in [#1](https://github.com/nlgfc2024/openimis-be-appraisal_py/issues/1). This
version holds the configuration, the rights, the data model and the pre-fill from imported applications.

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
configuration is checked when it is saved, and an invalid one is refused; every problem is reported with its
path, e.g. `programmes.UMEP.eligibility[2].key`. It is checked again at start: a saved row that is invalid (written
around the save check, or by an older version of the module) is logged as an error and disables the module's
programmes. The backend still starts, the rights fall back to their defaults, and the module's queries and
mutations answer `appraisal.not_configured` with the problems until the configuration is fixed.

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
| `eligibility` | The prerequisite checklist. Each item has a unique `key` and a `label`, and either a `strategy` (a calcrule_validations strategy run on `field`, with `params` and optional `location_type`), a `check` (`youth_ownership`, `co_financing`, with `params`), or neither for an item only an officer judges. See "Pre-fill from the application". |
| `inputs` | Score inputs derived from answers and documents (`months_since`, `all_answered`, `document_stored`). |
| `scoring` | Score components: `key`, `label`, `field`, `weight` and `scoring` in enrolment's weighted-score shape (`mapping` or `numeric_bands`); the total is normalised to `normalise_to` (default 100). |
| `shortlist` | Places: `size` overall, and/or `per_city` places by city. |

Strategy and check names must exist; score rules are checked for shape here and in full by the scoring.

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

## Pre-fill from the application

An application imported or updated by msr_etl (`kobo_submission_applied`) gets an appraisal for each programme
that reads its source, with one `ELIGIBILITY` item per checklist rule, proposed from its answers:

- **`strategy`**: a calcrule_validations strategy run on the answer to `field`, with `params` and the submission
  date as `reference_date` (ages, expiry dates); a `reference_date` in `params`, such as a round's cut-off date,
  replaces it. Success gives `YES`, failure `NO`; the basis keeps the answer and
  the strategy's note. With `location_type`, a location code is first followed to its current code
  (msr_etl `current_location_code`), so an answer given with a since-replaced temporary code still matches.
- **`check`**: a built-in check reading an uploaded template; the template is the applicant's word, so the result
  stays `PENDING` with the proposal (`proposed`: `YES`/`NO`) and the figures in the basis.
  - `youth_ownership`: a sole proprietor (`legal_form_field` answered with one of `sole_forms`) aged
    `min_age`-`max_age` on `dob_field` is `YES`, otherwise `NO`; a partnership or company is proposed `YES` when
    owners of that age hold more than `min_share`% in `template`, read from `columns` `age` and `share`.
  - `co_financing`: proposed `YES` when the applicant contributes at least `min_share`% of the applicant and
    project contributions in `template` (`columns` `applicant` and `project`), with the answer to the optional
    `sources_field`. A filled contribution cell that isn't a number leaves it `PENDING`, never 0.
  - Every parameter but `sources_field` is required and has no default, so each programme states its own rules;
    see the UMEP configuration in coremis-configs.
- **neither**: `PENDING` for the officer.

A missing answer, or a template that is missing, still downloading or unreadable, is `PENDING` with the reason,
never `NO`.

**Score inputs.** The value of every score component's `field` is stored in `json_ext.score_inputs`: the answer,
or a value the programme's `inputs` derive:

```json
"inputs": {
  "months_in_operation": {"derive": "months_since", "field": "business_start_date"},
  "has_business_bank_account": {"derive": "all_answered", "fields": ["business_account_name", "business_bank", "business_account_number"]},
  "has_bank_statement": {"derive": "document_stored", "document_types": ["bank_statement_3m"]}
}
```

`months_since` counts whole months to the submission date; `all_answered` is true when every question is
answered; `document_stored` is true once a document is stored, unknown (null) while one is downloading.

**Refreshing.** A repeat application, or a document stored later (`applicant_document_stored`), refreshes the
system's values under the rules in force when the appraisal was created (`config_snapshot`). An officer's answer
is never changed; an item whose basis changed after an officer answered it, or once the eligibility decision is
taken (`ELIGIBLE` onwards), is flagged `basis_changed` for review. Nothing is written when nothing changed. An
older application never replaces a newer one: applications are ordered by submission time, which holds across
Kobo forms, then by Kobo id.

**Backfill.** `manage.py appraisal_backfill [--programme UMEP] [--dry-run]` appraises applications imported before
the module or a programme was configured, from each person's latest application. It is safe to run again.

## Desk screening

An officer answers checklist items and records the eligibility decision (Operations Manual 8.2, Annex 2A):

- **Answer** an item `YES`, `NO` or `PENDING`. `YES` and `NO` need the evidence or source checked; `PENDING`
  needs the clarification asked for. The answer is recorded against the officer and time and clears
  `basis_changed`; the appraisal moves to `DESK_SCREENING`.
- **Decide** from the answers that count (the officer's, else the system's): any `NO` is `INELIGIBLE` (stage
  `CLOSED`); otherwise any `PENDING` is `CLARIFICATION` (screening stays open); otherwise `ELIGIBLE` (stage
  `DESK_SCORING`). Every rule needs an item, and an item whose basis changed must be answered again first. Each
  decision is added to `json_ext.eligibility_decisions` with the officer, time, comment and the items behind it.
- Items can be answered, and a decision recorded, only while screening is open (`RECEIVED`, `DOCUMENT_CHECK`,
  `DESK_SCREENING`).
- **Reopen** an `ELIGIBLE` or `INELIGIBLE` appraisal to correct a wrong decision, with a reason (the return
  right): the decision is cleared, the appraisal goes back to `DESK_SCREENING`, and the reason is added to
  `json_ext.reopened` with who and when. An appraisal already scored or shortlisted can't be reopened.

## GraphQL API

```graphql
query {
  appraisal(programme: "UMEP", status: DESK_SCREENING, search: "banda",
            answers: ["primary_sector=energy"], first: 20, orderBy: ["-date_updated"]) {
    totalCount
    edges { node {
      uuid programme round stage status eligibility score rank koboId submissionId
      individual { uuid firstName lastName }
      items(kind: "ELIGIBILITY") {
        uuid key label systemResult basis basisChanged officerResult evidence comment decidedBy decidedAt
        result isOverridden
      }
      history { date user subject changes }
    } }
  }
}
```

- Filters: `programme`, `round`, `stage`, `status` (also `stage_In`, `status_In`), `eligibility` (the last three are enums, written unquoted), `individual_Id`, and `id` (the appraisal's UUID); `search` (name
  or national ID); `answers` (`key=value` on the applicant's answers, matched as text, and as a number when the
  value is one); `parentLocation` + `parentLocationLevel` (individual's location rule); `needsReview` (an item's
  basis changed). Needs `gql_query_appraisal_perms` and individual's `gql_individual_search_perms` (159001), since
  an appraisal shows the applicant's registry record and answers; a user sees the appraisals of the individuals
  in their locations.
- What the user may do now, worked out by the same code as the mutations so the page never repeats the rules:
  `canScreen` (answer the checklist), `canDecide` (record the decision), `canReopen`, and `decisionPreview`
  (`decision` the checklist leads to, the `ineligible`, `pending`, `unanswered` and `changed` keys, and `problem`,
  the refusal that stops the decision now).
- `items` come in the order of the appraisal's rules, with each rule's `label`; `result` and `points` are the
  values that count. `history` lists the changes to the appraisal and its items (field, old, new), newest first.
- The application's answers and documents are read with msr_etl's `koboApplications(individualId)` and
  `applicantDocuments(individualId)`, under msr_etl's own rights.

| Mutation | Input | Right |
| --- | --- | --- |
| `answerAppraisalItem` | `id` (item), `result` (`YES`/`NO`/`PENDING`), `evidence`, `comment` | `gql_mutation_screen_appraisal_perms` |
| `decideAppraisalEligibility` | `id` (appraisal), `comment` | `gql_mutation_screen_appraisal_perms` |
| `reopenAppraisal` | `id` (appraisal), `reason` | `gql_mutation_return_appraisal_perms` |

Mutations are logged and run like every openIMIS mutation (`mutationLogs` by `clientMutationId`); a refused one
reports why, e.g. `appraisal.evidence_required` or `appraisal.answers_changed`.

## Rights

| Right | Code | Config key |
| --- | --- | --- |
| View appraisals (with individual search, 159001) | 220001 | `gql_query_appraisal_perms` |
| Screen (record checks, decide eligibility) | 220002 | `gql_mutation_screen_appraisal_perms` |
| Score | 220003 | `gql_mutation_score_appraisal_perms` |
| Shortlist | 220004 | `gql_mutation_shortlist_appraisal_perms` |
| Review documents | 220005 | `gql_mutation_review_document_perms` |
| Return for action, reopen a decision | 220006 | `gql_mutation_return_appraisal_perms` |
| Approve | 220007 | `gql_mutation_approve_appraisal_perms` |

The 220000 block is reserved for this module; a test fails if another installed module defaults to one of
these codes. Rights are given to roles in the usual openIMIS role administration.

## Testing

From `openimis-be_py/openIMIS`: `python manage.py test appraisal --keepdb`.
