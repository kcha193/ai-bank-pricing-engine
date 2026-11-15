# Phase 2b verification: term-deposit specials and extraction completeness (2026-11-15)

Findings: DATA-001 (P1) and DATA-019 (P2) from
[2026-11-13-full-repository-audit.md](2026-11-13-full-repository-audit.md).
Base: `master` @ `09be914` (PR #72 merged).

## Root cause (DATA-001)

1. `competitor_rates/extractors.py` (`_rate_category_hint`) correctly tags a term-deposit row
   containing "special" as `rate_category="special"`.
2. The default deposit market resolves to `rate_category="term_deposit"`
   (`service._resolve_rate_category`), and `build_market_snapshot` drops every other category.
3. `orchestration.exit_code_for` counts only `term_deposit` rows as comparable. A bank with
   ordinary rows as well as a special is "comparable", so the run returned exit 0 SUCCESS.
4. A term row whose rate could not be read was dropped with no trace
   (`parse_html_rates`: `if not rate_categories: continue`).
5. Per-category coverage loss was already exempt from withdrawal detection, but it was never
   surfaced in the exit code.

## Fixture reproduction (`docs/audits/2026-11-13-evidence/ingest/td_special_mixed.py`, unedited)

| | Exit | Failures | 12m benchmark |
|---|---|---|---|
| Before (`09be914`) | 0 SUCCESS | `[]` | MARKET_LEADER 4.05 (BNZ, Kiwibank). The 4.50% ASB special was silently ignored |
| After | **2 PARTIAL_SUCCESS** | `special_comparability_uncertain`: `12m 4.5%: no_public_availability_evidence` | 4.05. The special is stored and tagged, **not** promoted |

This is **Option B**: the row says only "12 Months Special" and gives no evidence of who can
get the rate. The script's last printed line is fixed text in the original evidence. It no
longer describes the outcome, which is now explicit.

Option A is exercised by synthetic fixture C. That special says "Available to all new and
existing customers" and has the same balance band and frequency as the ordinary rate. It is
admitted, becomes the 12m leader at 4.45%, keeps `rate_category="special"`, and the strategy
rationale names it.

## Policy

See `docs/pricing_intelligence.md` → "Term-deposit specials and extraction completeness".
In summary, each special gets one of three statuses from the evidence on its own record:

- `comparable`: admitted to the benchmark.
- `not_comparable`: excluded; the run is not degraded.
- `uncertain`: excluded; the run exits 2.

There is no schema change. Old records are assessed the same way.

## Verification

- `ruff check .`: clean.
- `pytest -m "not live_rates" -q`: **2922 passed, 2 skipped**. The baseline before the change
  was 2871 passed, 2 skipped. The new tests are the 51 in `tests/unit/test_term_deposit_specials.py`.
- Coverage: **96.85%** total (gate 95%). `deposit_specials.py` 100%.
- The five live captures of 2026-10-30 still give exit 0 for both products, with every bank
  `TRUSTED_COLLECTION_SUCCESS`.
- Pricing economics: no file under `optimization/`, `pricing/`, `scenarios/` or `synthetic/`
  changed. The `summary.json` / `grid_results.csv` regression tests pass.

## Live verification: PENDING

No live collection was run, because it was not authorised in this session. The roadmap
requires a supervised, dated check of real term-deposit page structures before Phase 2b is
fully closed. The check covers specials, tiers and parser completion per bank, and it must be
done with a scratch database, not the operational one.

## PR #73 review follow-up (2026-11-15)

Two P1 findings from the automated review of `e18dba7`:

1. **Historical snapshots disagreed with the live benchmark.** `_row_to_history_record()`
   replaced every historical `raw_text_context` with a placeholder, so reassessing a special
   as of an earlier run found no availability evidence. Fixture C gave ASB 4.45% live but
   4.00% as of `run-0`, and 4.00% in the history series. Fix: for term-deposit specials,
   `source_text` is a material field (`store._keeps_historical_evidence`), so each stored
   state holds the text that was valid for its whole lifetime, and history reads return it.
   The HTML extractor already stores that text in the material `eligibility_text`, so no
   extra states are created in practice. The history series now also takes dates from
   special states and withdrawals. There is no schema change and no migration.
2. **PIE-table specials could be admitted.** The table-level PIE signature ("Rate of
   return", "Return Frequency") converted only `term_deposit` rows. A "12 Months Special" row
   in such a table stayed `special` and, with "Available to all customers", led the market.
   Fix: the extractor converts specials in a PIE return table to `pie_term_deposit`, like the
   per-cell "pie" hint. The assessment also marks a stored special that carries the signature
   `not_comparable` (`pie_return_table`), using the same pattern (`is_pie_return_text`).

Codex re-review of `6a5032c` raised two more:

3. **Negated availability counted as evidence (P1).** "Not available to all customers"
   matched `available to all` and `all customers`, so the special was admitted. Fix: a
   negation ("not", "no", "never", "unavailable", "isn't", ...) in the two words before an
   availability phrase, in the same clause, makes it `not_comparable`
   (`negated_availability:<phrase>`). "No fees. Available to all customers" is still positive.
4. **Health and exclusions ignored admitted specials (P2).** `health.comparable_records()`
   filtered on the exact category, so a bank whose only benchmark rate was an admitted special
   showed freshness `UNKNOWN`, and its stale special produced no exclusion. Fix:
   `comparable_records(admitted_keys=...)` mirrors `build_market_snapshot`, and health and
   `get_market_exclusions()` pass `service.admitted_special_keys(...)`.

Verification is recorded in the PR description and review replies; live verification is
still pending, as above.
