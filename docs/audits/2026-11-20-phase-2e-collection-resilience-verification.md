# Phase 2e verification: collection deadlines, browser readiness and SQLite reliability (2026-11-20)

Findings: DATA-007, DATA-008, DATA-009 and DATA-010 from
[2026-11-13-full-repository-audit.md](2026-11-13-full-repository-audit.md).
Base: `master` @ `77b4d74` (PR #75 merged). Evidence:
[`2026-11-20-phase-2e-evidence/`](2026-11-20-phase-2e-evidence/). Everything here was verified
offline: stub collectors, local HTML pages served from `127.0.0.1`, temporary SQLite files and
real child processes. **No live bank page was fetched. LIVE VERIFICATION PENDING.** Phase 2b
term-deposit live verification is a separate item and also remains pending.

## Root causes

- **DATA-007 readiness.** The browser fetcher waited for *any* `td`/`th` containing a `%`
  (`RATE_TABLE_READY_JS`), for any product. A home-loan page showing one term-deposit cell, or
  a half-rendered table, counted as ready. A readiness timeout was swallowed
  (`except PlaywrightTimeoutError: pass`), so a slow render surfaced later as a generic
  `ParserError`, or as a short-but-accepted rate set.
- **DATA-008 deadline.** No end-to-end bound existed. A hung synchronous Playwright call blocked
  the run indefinitely while it held the product lock, so each later schedule exited 3 with no
  escalation. macOS ships no GNU `timeout` to wrap it.
- **DATA-009 SQLite.** `ObservationStore` used the `sqlite3` default 5 s timeout and rollback
  journal. A concurrent writer past the timeout raised `OperationalError` out of
  `run_collection()`, which caught only `ChronologicalOrderError`: traceback, no summary file,
  no classification.
- **DATA-010 CLI collect.** `pricing_intelligence collect` called `service.collect()` with no
  lock and no RunRecord. It could interleave with `run` or the scheduler on the same database,
  and exited 0/1 on its own rules.

## Changes

| Concern | Before | After |
|---|---|---|
| Readiness signal | any `%` in a table cell | product named in title/headings/captions **and** at least `readiness_min_rate_rows` rows from the bank's own parser |
| Wrong-product page | ready | `WRONG_PRODUCT`, never ready |
| Readiness timeout | swallowed | `ReadinessTimeoutError` / `PartialRenderError`, per bank, retried, then a classified failure |
| Navigation failure | generic `FetchError` | `NavigationError` |
| Run bound | none | `collection_run_timeout_seconds` (1800 s), cooperative checks plus a supervisor process that kills the worker tree |
| Timeout evidence | none | atomic summary with `failure_type=collection_timeout`, run id, start, deadline, elapsed; FAILED RunRecord when the DB allows; `run_record_persisted` states which |
| Repeated exit 3 | silent | per-product sidecar streak: warning at 2, one alert per channel at 3, recovery notice, reset |
| SQLite | rollback journal, 5 s | WAL on file-backed writable stores; bounded busy timeout (30 s default, max 300) |
| Lock error | uncaught traceback | `database_locked` (BUSY/LOCKED only) or the SQLite class name; exit 1; summary checks committed evidence, preserving earlier transactions |
| Summary writes | `write_text` | temp file, fsync, `os.replace` |
| CLI collect | no lock, no ledger | shared product lock before a writable store opens; exit 3 with no fetch; `run_operational_cycle`; `--no-persist` lock-free read-only preview |
| Dashboard live run | subprocess timeout 1800 s, no run deadline | passes `--run-timeout-seconds` 60 s below its own timeout so the supervisor, not a SIGKILL, ends a hung run |

### Readiness thresholds (evidence-based)

Defaults are about half the comparable rows each bank's saved live renders produced
(`data/raw/competitor_rates`, git-ignored, Sep-Nov 2026). Every saved page title named its own
product and never the other one. Zero-row renders on 2026-09-03 and 2026-09-08 show the
incomplete-render failure mode the old check could not distinguish.

| Bank | HL rows seen | HL minimum | TD rows seen | TD minimum |
|---|---|---|---|---|
| ANZ | 14-16 | 7 | 30 | 15 |
| ASB | 9 | 5 | 13 | 6 |
| BNZ | 11 | 5 | 31 | 15 |
| Kiwibank | 15 | 7 | 22 | 11 |
| Westpac | 17 | 8 | 28 | 14 |

### Deadline derivation

`estimated_worst_case_run_seconds(CollectionConfig(), bank_count=5)` = 1264 s: per bank, every
attempt's request timeout and readiness wait, backoff between attempts, the inter-bank delay,
plus 10 s browser start-up and 180 s post-collection allowance. The 1800 s default sits above it.
`tests/unit/test_deployment_config.py` asserts every schedule's deadline exceeds that figure.

## Evidence

### `db_locked.py` (original audit script, unmodified)

`docs/audits/2026-11-13-evidence/ingest/db_locked.py` was run as-is
([`db_locked_original_after.out`](2026-11-20-phase-2e-evidence/db_locked_original_after.out)):

```text
raised OperationalError: database is locked after 31.2s -- run_collection() only catches ...
operational_runs after: [('SUCCESS',)]
connect() timeout used by ObservationStore: default (5.0 s); journal_mode: wal
```

The script calls `service.collect()` directly, not the workflow, so it still raises. That is
correct: the service layer propagates, and the workflow classifies. Its trailing explanation and
the "default (5.0 s)" label are static strings in the script. The measured values changed:
the wait is now 31.2 s (the 30 s busy timeout) and `journal_mode` is `wal`. The only
`operational_runs` row is the earlier successful setup run: the locked run committed nothing.

### Same scenario through the workflow

[`db_locked_workflow.py`](2026-11-20-phase-2e-evidence/db_locked_workflow.py) runs the same
contention through `run_collection()` with a 2 s busy timeout
([`db_locked_workflow.out`](2026-11-20-phase-2e-evidence/db_locked_workflow.out)):

```text
exit code: 1 after 2.1s (busy timeout 2.0 s)
summary file written: True
  failure_type: 'database_locked'
  error_type: 'database_locked'
  run_id: 'db-locked-replay'
  product_type: 'term_deposit'
  run_record_persisted: False
  run_record_note: 'the observation store was locked by another writer past the busy timeout; ...'
  banks_failed: ['*']
operational_runs after: [('92d8b84b55c147538553699d94647997', 'SUCCESS')]
observations rows unchanged by the locked run: True
journal_mode: wal
```

### Hard deadline (real processes)

`tests/unit/test_collection_resilience_workflow.py` starts a real worker that hangs, with a
child process standing in for Chromium. The worker ignores SIGTERM in one variant. After the
deadline the supervisor terminates and reaps the whole tree (no surviving pid), writes the
`collection_timeout` summary, records one FAILED run, and releases the lock: a second run
acquires it immediately. Another test holds the database's write lock through the timeout and
checks the summary reports `run_record_persisted: false`. A full worker round trip runs
`configs/base.yaml` with no selected banks through the real subprocess path.

## Tests (T01-T44 mapping)

| Area | Tests | File |
|---|---|---|
| T01-T08 readiness: product evidence, row minimum, wrong product, async/XHR render, partial, timeout, navigation, per-bank isolation | 26 (2 drive real Chromium on a local `http.server`) | `test_collection_browser_readiness.py` |
| Fetcher fallback, script-text rates not counted | 3 | `test_browser_readiness.py` |
| T09-T20, T27, T37, T38 deadline, fake clock, supervisor tree kill, no write after timeout, summaries, skip escalation, dedupe, recovery, `database_locked` summary, product isolation, repeated run id | 33 | `test_collection_resilience_workflow.py` |
| T21-T26, T28-T29 WAL, busy timeout bounds, in-memory unchanged, read-only copy, backup single file, lock classification vs corruption/disk/permission/schema, atomic rollback | 26 | `test_pricing_intelligence_sqlite_reliability.py` |
| T30-T36 CLI collect lock, exit 3 no fetch, `--lock-file`, shared with `run`, RunRecord, partial/total, TD own lock, `--no-persist` preview, `database_locked` | 11 | `test_pricing_intelligence_cli_collect_locking.py` |
| T39-T44 deployment deadline in both plists and crontab, `ExitTimeOut`, dashboard deadline margin, existing exit-code consumers | 3 | `test_deployment_config.py`, `test_dashboard_actions.py` |

Changed expectations in existing tests, each with a reason:

- `test_scheduled_collection_workflow.py`: in-process `main()` tests that monkeypatch module
  state now pass `--no-supervisor`; a supervised worker is a separate interpreter.
- `test_pricing_intelligence_cli.py::test_collect_handler_reports_validation_issues`: exit 2,
  not 0. `collect` now shares `run`'s classification, where rejected rows are a partial run.
- `test_browser_readiness.py`: rewritten for the product-aware check (the old JS predicate is gone).

## Verification results

| Command | Result |
|---|---|
| `python -m ruff check .` | All checks passed |
| `python -m pytest -q -m "not live_rates" --cov=balance_pricing_engine --cov-fail-under=95` | 3365 passed, 2 skipped; coverage 96.61% (baseline before Phase 2e: 3269 passed, 2 skipped) |
| Focused re-run after the dashboard deadline change (dashboard, deployment, workflow, CLI collect suites) | 294 passed |
| `plutil -lint deploy/scheduling/*.plist` | both OK |
| `git diff --name-only` over `optimization/`, `pricing/`, `synthetic/`, `governance/`, `modelling/`, `configs/` | no files: pricing economics unchanged |
| New module coverage | `readiness.py`, `run_deadline.py`, `run_artifacts.py`, `sqlite_errors.py` 100%; `skip_escalation.py` 99%; `supervisor.py` 88% (uncovered: `ps` unavailable, `PermissionError` paths) |

Browser tests ran against a locally installed Chromium (the optional `[browser]` extra). On a
machine without Playwright the two real-Chromium tests skip and must be reported as unverified.

## Backward compatibility

- Exit codes 0/1/2/3 keep their meanings; 1 now also covers classified whole-run failures.
- Summary files keep every existing key; added keys are additive (`run_id`, `status`,
  `failure_type`, `run_record_persisted`, and the timeout fields).
- WAL remains a persistent journal-mode setting. PR review adds an additive revision table
  and triggers on writable initialisation; existing observation/review evidence is preserved.
  Dashboard reads do not install or migrate schema.
- Existing CLI flags are unchanged. `collect --no-persist` output now ends with a `Preview:` line.
- `--no-supervisor` restores in-process execution with only the cooperative deadline.
- No pricing mathematics, response model, optimiser or governance gate changed.

## Remaining risks

- Thresholds come from saved renders. A bank redesign that legitimately lists fewer terms will
  fail readiness for that bank (exit 2) until its `readiness_min_rate_rows` is lowered with
  evidence.
- The supervisor finds descendants with `ps`. If `ps` is unavailable it falls back to a
  process-group kill, which misses a Chromium started in its own session.
- If the supervisor itself is SIGKILLed, its worker survives until it finishes or reaches a
  cooperative checkpoint. The inherited lock makes later runs skip and escalate rather than
  overlap.
- WAL does not remove single-writer contention. Two products writing at once still serialise
  through the busy timeout.
- LIVE VERIFICATION PENDING for Phase 2e and, separately, for Phase 2b term deposits.

## PR #76 review remediation (2026-11-20)

### Finding inventory

Initial head: `d78fa821cf39746548af89d3c8da67dc9c1f8acf`. Live GitHub inspection included
all files/diff, submitted reviews, top-level comments, resolved/outdated inline threads and
check runs. Pagination reported no additional threads. One actionable GitHub thread existed;
an independent whole-branch review found the additional issues below.

| Severity / source | Location and root cause | Correction | Regression evidence |
|---|---|---|---|
| P1, [comment 4639633384](https://github.com/kcha193/ai-bank-pricing-engine/pull/76#discussion_r4639633384), thread `PRRT_kwDOSMlm-s654Rnx` | `DashboardServices.db_stamp`: main-file mtime misses WAL commits | Transactional revision triggers; read-only live readers; unknown/legacy provenance hidden; capture before calculation | `test_dashboard_database_stamp.py`, `test_dashboard_result_freshness.py`: real WAL visibility with writer open, unchanged main mtime, stale outcome and absent Studio output in all three modes |
| P2, independent review | `assess_page_readiness`: PIE return rows counted towards ordinary deposit readiness | Exclude `pie_term_deposit` alongside `non_comparable`; uncertain-special collection semantics unchanged | Real ANZ, BNZ and Westpac parsers on local PIE tables; all remain NOT_READY |
| P2, independent review | `run_collection`: reused original status combined with retry fetch evidence | Original RunRecord drives top-level evidence; retry evidence is separate; unavailable original details are null | Successful original / failed retry, partial original / successful retry, committed observations without original RunRecord |
| P2, independent review | `_database_failure`: a later bookkeeping error falsely claimed earlier observations rolled back | Read-only committed-participation probe; true/false/unknown evidence and surviving counts | Injected RunRecord lock error after a real observation commit; exit 1 preserves committed count |
| P2, boundary verification | CLI preview: `persist=False` still permitted raw HTML snapshots | Disable snapshot persistence for `collect --no-persist` | Real collector with injected local HTML leaves no raw directory |

All five corrections passed their focused regression checks. The independent reviewer then
reported no remaining actionable findings. GitHub thread closure and fresh-head review are
tracked on the PR itself; this local record does not claim a pending remote review completed.

### Reproduction and design

The initial regression opened a real temporary WAL ObservationStore, committed an observation,
checkpointed, captured the dashboard stamp, and committed a second observation while keeping
the writer open. A second SQLite connection saw both collection rows; the main database mtime
was unchanged. The original stamp failed with:

```text
AssertionError: committed WAL data must invalidate the dashboard stamp
1 failed
```

The expanded initial file gave 6 failed / 3 passed against the old stamp. A separate offline
mutation check restored the exact baseline `db_stamp` and freshness functions in memory:
the core WAL regression and all three Studio modes failed (4 failed, 39 deselected), with
stale metrics still present. No production file was replaced for that check.

Approaches considered: main/WAL metadata cannot identify committed state across rollback and
reuse; a fresh `data_version` is connection-local; retained global connections violate the
lifecycle boundary; full database hashes/copies cost too much per rerender; raw WAL parsing
would duplicate SQLite's concurrency/recovery machinery. The selected additive schema is one
singleton row and three row triggers per application table. SQLite commits the random token
atomically with data. Exact trigger coverage is checked in the same short read transaction as
the token and schema version. Device/inode/path identify database replacement. A writer installs
tracking after migrations; dashboard reads never migrate the source.

Checkpoint, reset, reuse, truncation and removal do not erase the committed token. Uncommitted
and rolled-back tokens are invisible to readers. Missing/invalid tracking or unreadable data
is unknown, never CURRENT and never a reusable cache key. Results remain in session state but
are hidden until explicitly recalculated against current inputs.

SQLite `mode=ro` may create/manage WAL/SHM coordination files. The owner explicitly authorised
that native coordination while requiring database data and schema to remain read-only. Both
stamp and actual dashboard reads use short-lived read-only connections, bounded at 100 ms;
there is no per-render database copy, migration, checkpoint or global connection.

Limitations: tracking updates once per mutated row; invalidation is conservative across the
shared store; 128-bit random-token collision risk is negligible rather than impossible;
an administrator deliberately rewriting data and tracking together is outside this mechanism.
Freshness is checked at render time, not locked for the lifetime of an already displayed page.
No pricing economics, governance policy, response models or product data boundaries changed.

### Review verification

Commands use the existing `.venv` (system Python has no pytest). No public bank website was
contacted. Both deployment plists pass `plutil -lint`; baseline and response-model examples
complete successfully. Ruff and `git diff --check` pass. New provenance files pass Ruff format.

The first broad run stopped after one outdated test simulated collection with `os.utime`:
1 failed, 1,643 passed, 1 skipped before interruption. A focused reproduction identified
`test_a_result_is_hidden_when_the_market_data_changes_underneath_it`; it now commits a real
observation update. The corrected test passes. The complete offline suite is rerun below.

| Final command/check | Actual result |
|---|---|
| `.venv/bin/python -m pytest -q tests/unit/test_dashboard_database_stamp.py tests/unit/test_dashboard_result_freshness.py tests/unit/test_collection_browser_readiness.py tests/unit/test_collection_resilience_workflow.py tests/unit/test_pricing_intelligence_cli_collect_locking.py` | 139 passed in 58.91 s |
| `.venv/bin/python -m pytest -m "not live_rates" --cov --cov-fail-under=95 -q --durations=10` | 3,402 passed, 2 skipped; 98.04% whole-run coverage; 434.94 s |
| `.venv/bin/python -m coverage report --precision=2 --include='src/balance_pricing_engine/*'` | Package coverage 97.05%; `revision.py` 100% |
| `.venv/bin/python -m ruff check .` | All checks passed |
| `.venv/bin/python -m ruff format --check src/balance_pricing_engine/pricing_intelligence/revision.py tests/unit/test_dashboard_database_stamp.py` | Both files formatted |
| `git diff --check` | Clean |
| Baseline and response-model examples; both launchd plist checks | Passed |

Live collection remains unauthorised and unverified. PR #76 has not been merged.
