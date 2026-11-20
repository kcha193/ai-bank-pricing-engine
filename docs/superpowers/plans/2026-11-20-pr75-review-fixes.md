# PR #75 review fixes

Branch: `fix/phase-2d-legacy-ingestion-hardening`.
Starting head: `ac8a84a8ae0dfd657c1fbd598ac574fc40e3b232`.
Scope: synthetic, offline regression coverage and the smallest deposit-selection correction.

## Findings tracker

| Comment ID / URL | Reviewer | Severity | File / line at review | Root cause | Correction | Regression requirement | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| [4596252540](https://github.com/kcha193/ai-bank-pricing-engine/pull/75#discussion_r4596252540) | chatgpt-codex-connector | P1 | `competitor_rates/apply.py:441–442` | The specials-only path selected by observation key before aggregation, discarding a higher rate at equal frequency precedence. | Opt into highest-rate selection only for specials, after frequency and exact-match precedence; break equal rates by observation key. | T01–T10 in `test_competitor_rate_legacy_hardening.py`. | Fixed; local verification and independent review passed. GitHub thread update follows push. |
| [CI run 44033426596](https://github.com/kcha193/ai-bank-pricing-engine/actions/runs/44033426596) (not a review comment) | GitHub CI | Verification blocker | `test_competitor_rate_legacy_hardening.py::test_t29_one_row_per_bank_inputs_keep_their_former_rates` | A decimal literal was used as an exact pre-PR arithmetic baseline. Python 3.11 and 3.12 float summation differ by one ULP. | Calculate the expected baseline from the fixture's explicit rates using the original `sum(rates) / len(rates) / 100` expression. Keep zero tolerance and all production arithmetic. | Run unchanged exact assertions on Python 3.11 and 3.12. | Fixed; exact assertions pass on both interpreters. |

## Implementation plan

1. Inspect all PR discussion, submitted reviews, thread states, commits, files and current diff.
   Confirm a clean checkout on the existing PR branch. Check existing CI failures.
2. Add parameterised synthetic regressions before changing production code. Prove 4% is selected
   instead of 5%, including reversed input and propagated strategy results.
3. Add a keyword-only selection option to `_select_frequency`, defaulting to existing behaviour.
   Only `_deposit_representative`'s empty-ordinary branch opts in. Keep frequency ordering,
   exact-before-combined precedence, ordinary selection, special eligibility, trust and coverage.
4. Correct the interpreter-dependent baseline expectation without changing assertions or pricing.
   Update architecture, assumptions and ingestion documentation to describe the selection rule.
5. Run focused tests, Ruff, the full offline suite and the documented 95% coverage gate.
   Inspect the full diff and obtain an independent review before committing and pushing.
6. Reply to the original thread with regression evidence and commit; resolve it only after verified.
   Refresh all feedback and CI at the new head. Request one Codex review if none is pending.
   Address subsequent findings with TDD, up to three review/fix cycles. Never merge.

## Review focus

- Ordinary deposits must retain observation-key selection, including combined and fallback cases.
- Specials must preserve frequency priority and exact-before-combined matching before comparing rates.
- No matching preference is a fallback group: choose its highest special, then observation key.
- Uncertain, restricted and untrusted readings cannot supply a reference or inflate bank coverage.
- Baseline arithmetic must be bit-identical to pre-PR arithmetic within each supported interpreter.

## Execution record

- Existing focused baseline: 94 passed on Python 3.12.
- T01–T10 RED: 14 failed, 31 passed before the production change. Failures demonstrate the
  reported 4%/5% selection, strategy propagation, combined-frequency and fallback defects.
- Ruling: continue in the clean, explicitly requested PR branch; no extra worktree or branch is needed.
- Ruling: test only supported TD strategies (`bank_specific`, `market_max`, `market_average`,
  `market_median`). The existing TD configuration deliberately excludes `market_min`.
- Ruling: fix the Python-dependent test oracle rather than alter production float arithmetic or
  relax tolerance. Changing pricing arithmetic would expand scope and risk backward compatibility.
- GREEN: 143 focused tests passed on Python 3.11 and 3.12 (123 hardening, 20 apply).
  Ruff passed in both environments. The final regression addition contains 49 parameterised cases.
- Independent read-only review: no critical, important or minor findings. An independent oracle
  checked 1,200 frequency/preference/order combinations and exact ordinary-selection compatibility.
  Live collection was outside the review's offline scope; full-suite results are recorded separately.
- Full offline Python 3.12 suite: 3,269 passed, 2 existing skips, exit 0.
- Full offline Python 3.11 suite with the documented coverage options: exit 0; coverage 96.94%
  against the unchanged 95% gate. `competitor_rates/apply.py`: 100% statement coverage.
- Final diff review: six scoped files; no credentials, real customer data, generated artifacts,
  golden-file changes, new skips, weakened assertions or threshold changes.
