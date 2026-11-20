# Phase 2f verification: deposit benchmark comparability and eligibility

Date: 2026-11-21. Branch: `fix/phase-2f-benchmark-comparability`. Findings: DATA-005, DATA-013,
DATA-014 (`2026-11-13-remediation-roadmap.md`, row 2f). Offline only.

## Root causes

| Finding | Root cause |
| --- | --- |
| DATA-005 | `build_market_snapshot` kept each bank's most competitive rate per term across every balance tier and payment frequency. |
| DATA-013 | `enrich_mortgage_record` set `max_lvr=0.80`/`min_equity=0.20` from a special-rate label or an unmarked special-tier row with nothing distinguishing that from a published LVR. |
| DATA-014 | `our_bank` was an unvalidated string compared case-insensitively. "ASB Bank" did not match "ASB". |

## Changes

- `pricing_intelligence/deposit_reference.py` (new): `DepositReferencePolicy` (default $10,000,
  at maturity, no fallback frequencies, unstated tier/frequency admitted and disclosed) and the
  one eligibility function `assess_reference_eligibility`.
- `snapshot.py`: reference filter and a total-order representative selection (frequency match,
  rate, category, product name, observation key). `BankTermRate` gains eligibility evidence and
  `eligibility_note`. `MarketSnapshot` gains `deposit_reference` and `reference_exclusions`.
- `service.py` / `health.py`: every category-filtered TD view applies the policy (live, previous
  run, `as_of_run_id`, history, stale exclusions, health). `all_tiers=True` is the descriptive view.
- `competitor_rates/models.py`: `lvr_eligibility_basis()` and computed `eligibility_inferred`
  (derived, not stored, not in `observation_key()`), `offers_frequency()`, canonical bank
  validation on the legacy config.
- `competitor_rates/bank_identity.py` (new) plus `bank_aliases` on each collector:
  canonicalisation for `our_bank`/`selected_banks`. `snapshot._banks_match` uses it.
- `strategy.py` / `coverage.py`: rationale and `describe()` name banks with inferred or
  unestablished eligibility.
- Dashboard: an Eligibility column in Market Explorer and in the ranking frame. The label comes
  from the backend.

## Evidence

Original audit script `2026-11-13-evidence/ingest/td_tiers_and_legacy.py`, case (a), unchanged.
The trailing "ASB represented by its $500k+ tier" is a hard-coded string in the script, not a
result:

```text
BEFORE (master a1b8070)  snapshot 12m: [('ASB', 4.25, 1), ('BNZ', 4.05, 2), ('Kiwibank', 4.05, 2), ('ANZ', 4.0, 4), ('Westpac', 4.0, 4)] leader 4.25
AFTER  (this branch)     snapshot 12m: [('BNZ', 4.05, 1), ('Kiwibank', 4.05, 1), ('ANZ', 4.0, 3), ('ASB', 4.0, 3), ('Westpac', 4.0, 3)] leader 4.05
```

`2026-11-21-phase-2f-evidence/phase_2f_reproduction.py` (output in `.out`):

- $10,000: ASB 4.00. Best 4.05 (BNZ, Kiwibank); median 4.00.
- $500,000: ASB's 4.25 tier is eligible and leads the computed ranking. Median 4.05. The other
  banks' representatives are unchanged.
- The all-tier view keeps ASB at 4.25 with `deposit_reference=None`.
- DATA-013: ANZ "Fixed Special" gives max_lvr 0.8, `inferred`. Westpac "80% LVR" gives 0.8,
  `published`. Westpac plain gives `None`, `unknown`.
- DATA-014: `our_bank="ASB"` and `"ASB Bank"` give an identical median of 4.0250 over ANZ, BNZ,
  Kiwibank and Westpac. "ASB Banking Group" raises `ValidationError`.

Benchmark bank-count effect on the 2026-10-30 captures at the default: all five banks stay
eligible at 12 months. ANZ and BNZ (no tier, no frequency published) and ASB (no frequency) are
admitted with "Eligibility not established". With both `admit_unstated_*` flags set to false,
only Kiwibank and Westpac remain, which is below `min_banks=3`.

## Tests

- `tests/unit/test_phase_2f_benchmark_comparability.py`: 55 tests (T01–T39, plus the audit
  reproduction, the dashboard label and alias-registry integrity). T18–T20, T40 and T41 are also
  covered by the existing Phase 2a/2b/dashboard/API suites.
- Changed existing tests:
  - `test_competitor_rate_apply.py`: an unknown bank now fails at config load, and a new test
    asserts this. The no-match test uses an empty bank list.
  - `test_dashboard_services_market.py`: the TD fallback tier moved from a $20k to a $5k
    minimum. The $20k tier is exactly what DATA-005 now excludes for a $10k depositor.
- `ruff check .`: clean.
- `pytest -m "not live_rates" --cov --cov-fail-under=95`: **3465 passed, 2 skipped, 0 failed;
  coverage 96.92%.**

## Economics

The optimiser, elasticities, ECL, funding, ROE, gates, LLM allow-list, champion checks and the
Phase 2a–2e controls are untouched. The baseline golden test passes. A market-derived TD
benchmark can now differ (for example, the audit market's 12-month leader moved from 4.25 to
4.05). That is a change in which observations qualify, not a change to the equations. Adding
`deposit_reference` to `PricingIntelligenceConfig` changes the effective config hash of any
config that carries a `pricing_intelligence` block. That is intended lineage (T39).

## Live verification

**LIVE VERIFICATION PENDING** for Phases 2b, 2e and 2f. No live bank page was fetched. Phase 2
is implemented and tested offline, not operationally verified.

## Remaining risks

- Most live TD pages publish no payment frequency, so the default benchmark rests largely on
  "eligibility not established" admissions until collectors capture frequency headings.
- The legacy `competitor_rates` TD path keeps its own frequency preference and has no
  reference-balance filter. It is reachable only when `competitor_rates.enabled`.
- Market-activity counts (`get_market_changes`) still count moves across every ordinary tier.
- A strict-source JSON record that carries `max_lvr` without stating it in its text reads as
  `inferred`. This is conservative by design.
