# Phase 2a verification: quarantine recovery and rate-validation governance

Findings closed: **DATA-002** (P1, no recovery path for a quarantined reading) and **DATA-016**
(P2, a plausible +60 bps misread is accepted and cannot be withdrawn).
Branch `feat/phase-2a-quarantine-governance`, from master at `16cb94e` (PR #71 merged).
Audit evidence under `docs/audits/2026-11-13-evidence/` is unchanged; the corrected-behaviour
replay is a new script, `docs/audits/2026-11-15-phase-2a-evidence/case_Q_after.py`, which reuses the
preserved harness and fixtures. All runs are offline: temporary SQLite, no network, no live bank
collection, no alert delivery, no production database.

## Root causes

| | Cause |
| --- | --- |
| DATA-002 | A quarantined reading is stored with `validation_status='warning'` and every later run re-measures the new reading against the **last trusted** rate (a deliberate fix so repetition is not verification). So a genuine +100 bps repeats as +100 forever. `ObservationStore.confirm_suspicious_changes()` could clear the flag but had **no caller**, and picked "the most recent suspicious event" by key, so even a caller could not name an exact event. Nothing counted how long a reading had waited, so the run stayed exit 0 until the bank aged out of the market. |
| DATA-016 | The only plausibility control compared a reading with its own history (75 bps). Nothing compared it with the market, and once accepted a reading had no void path, so a +60 bps misread became `MARKET_LEADER`. |

## Data trust model

* A decision is a row in the append-only `quarantine_reviews` ledger (SQLite triggers refuse
  `UPDATE`/`DELETE`). Observation rows are **never rewritten**.
* Effective trust = stored `validation_status` overlaid with the latest decision **as of the view's
  time** (`store._effective_observations`). A historical `as_of_run_id` view therefore never shows a
  reading approved before the approval (invariant F), and a void applies from when it was made.
* `APPROVE` (PENDING to APPROVED) makes a reading trusted and reconciles its change events through
  `confirm_suspicious_changes`, now pinned to the exact event. `REJECT` (PENDING to REJECTED): never
  trusted. `VOID` (ACCEPTED or APPROVED to VOIDED): removed from current rankings, benchmarks, market
  state and change statistics; the trusted rate is rebuilt from the last valid predecessor while it is
  fresh, else unavailable. REJECTED and VOIDED are terminal and an identical repeat stays untrusted.
* Approval confirms evidence, not attractiveness. Nothing is approved automatically.

## Before and after

Before (audit, `case_Q.out`, master `16cb94e`):

```
Q6  accepted unflagged; changes [('ASB 12m term_deposit: 3.90% -> 4.50% (+60 bps)', False)]
    ASB12 (4.5, ..., False)  MARKET_LEADER 4.5            # misread leads the market, no void path
Q7  day 8..13: code 0, ASB12 (3.9, quarantined)            # exit 0 every day, no recovery path
    day 14..16: code 0, ASB12 None                          # ASB silently gone from the market
```

After (`case_Q_after.out`, same fixtures and clock):

```
Q6  changes [('ASB 12m term_deposit: 3.90% -> 4.50% (+60 bps)', 'suspicious')]  code 0
    peer: ASB flagged, 4 peers, median 4.025%, +47.5 bps vs median, +60 bps relative movement
    ASB12 (3.9, quarantined)  MARKET_LEADER 4.05           # pending: not in the trusted benchmark
    queue: (event 2, ASB, 3.9 -> 4.5, PENDING, peer_outlier ...)
Q6b peer signal OFF (the audit's unfixed acceptance): MARKET_LEADER 4.5
    void: ACCEPTED -> VOIDED   ->  ASB12 (3.9), MARKET_LEADER 4.05, activity 1 (only the genuine -10 bps)
Q7  unapproved: day 2,3 code 0 | day 4..8 code 2 PARTIAL_SUCCESS, escalated=1, runs_pending 3..7,
    MARKET_LEADER 4.05 throughout (never auto-approved); ASB12 falls to None only on day 8 when its
    last trusted rate passes the 168 h freshness window, and the run is exit 2 the whole time
Q7b approved on day 3 with evidence: PENDING -> APPROVED, ASB12 (4.9, trusted), MARKET_LEADER 4.9,
    next run code 0 with 0 new changes and 0 pending; history [(3.9->4.9, trusted), (4.0->3.9, trusted)]
```

**Q6 on the preserved fixtures.** Strict balance-band equality found **zero** comparable peers for ASB:
the banks advertise different retail minimums (none, $1,000, $5,000). That was a real gap in the first
design, so balance tiers are now bands of the advertised minimum (`balance_tier_edges`, default
10,000 / 100,000): retail minimums pool, $500k+ specials do not. With four comparable peers the +60 bps
reading is flagged. Deterministic synthetic fixtures cover thin, stale, dispersed and tiered markets.

## Test results

| Check | Result |
| --- | --- |
| `python -m ruff check .` | All checks passed |
| `python -m pytest -q -m "not live_rates"` | 2,845 passed, 2 skipped (baseline on master before this PR: 2,740 passed, 2 skipped) |
| `--cov=balance_pricing_engine --cov-fail-under=95` | 96.78% (gate 95%) |
| New test files | `test_pricing_intelligence_quarantine_review.py`, `..._peer_validation.py`, `..._quarantine_escalation.py`, `..._review_cli.py`, `..._quarantine_audit_replay.py` (+ `quarantine_support.py` harness) |

Requirement coverage: T01 `test_t01...`; T02 `test_t02_unauthorised...`; T03 `test_t03...`;
T04 `test_t04...`; T05 `test_t05...`; T06 `test_t06...`; T07 `test_t07...`; T08 `test_t08_concurrent...`
(two connections, one winner, one ledger row); T09 to T15 `test_t09`..`test_t15`; T16 two as-of tests
(approval and void time); T17 `test_t17...`; T18 `test_t18...`; T19 `test_t19...`; T20 to T22
`test_pricing_intelligence_peer_validation.py`; T23 to T28 `test_pricing_intelligence_quarantine_escalation.py`
and `test_t23_health_command...`; T29 and T30 the replay file; T31 `test_t31_migration...`; T32 to T34
the existing suites (change-detection idempotency, collection recovery, Phase 1 governance and API
security) run unchanged inside the full suite. Mutation checks (removing the review-time bound, the
repeat guard, the escalation upgrade, the void filter and the peer direction guard) each fail the
intended tests.

## Controls

* **Authentication.** `approve`/`reject`/`void` need `PRICING_INTELLIGENCE_REVIEW_CREDENTIALS`
  (`identity=<sha256 of token>`) and a token from `PRICING_INTELLIGENCE_REVIEW_TOKEN` or a no-echo prompt.
  No `--token` option exists. Fails closed when unset, malformed, too short or wrong. The recorded
  identity is the one the token authenticated. Secrets are never logged, echoed or put in an exception.
* **Integrity.** Mandatory reason (and evidence for approval), control characters refused, exact
  `--event-id`, transition table, idempotent repeats, `BEGIN IMMEDIATE` plus `UNIQUE (observation_id,
  previous_state)` against concurrent contradictory decisions, a review cannot predate its event.
* **Schema.** Additive (`CREATE TABLE IF NOT EXISTS`); a pre-2a database gains the ledger on open and keeps
  every observation (T31). `rate_changes` and the run-record schema are unchanged; `summary.json` and
  `grid_results.csv` (the optimiser regression contract) are untouched.

## Remaining limitations

* REJECT and VOID are terminal; there is no reinstate command. A wrongly rejected rate is only
  re-evaluated when the bank's reading changes.
* Attribution is token-based trusted-operator attribution, not per-person identity proof; rotating a
  credential means replacing its digest.
* A voided or pending rate falls back to the last trusted rate only while it is inside
  `max_observation_age_hours`; after that the bank is absent from the market (now loudly: exit 2).
* The peer check needs at least three comparable banks and a prior reading to measure movement. It abstains
  otherwise and is reported as `peer_insufficient_evidence`.
* A multi-hop chain across a category or eligibility change approves without rebasing onto a trusted
  predecessor (the existing conservative `confirm_suspicious_changes` behaviour).
* Dashboard pages are unchanged; quarantine state is available through `SystemHealth.quarantine`, the
  `health` command and the run summary.
* TD special-rate extraction (DATA-001/019) is Phase 2b and not addressed here.
