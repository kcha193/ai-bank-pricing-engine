# Post-Review Fixes Implementation Plan

> **STATUS: ✅ COMPLETE** — post-review follow-up fixes — PR #33 + #34

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Address the three remaining findings from the 2026-05-18 codebase review verification: stale baseline numerics in the usage guide (P0-4), a missing post-expansion assertion in the boundary diagnostics test (P1-2), and missing CLI tests for the `--max-staleness-hours` and `--previous-file` validate flags (P1-4).

**Architecture:** Pure fixes — no new modules, no API changes. Two tasks touch existing tests only; one task updates a markdown doc. The implementation for all CLI flags already exists; we are adding test coverage that was accidentally omitted.

**Tech Stack:** Python 3.9, pytest, `gh` CLI for verification only.

---

## File Map

| Task | File | Action |
|------|------|--------|
| 1 | `docs/usage_guide.md` | Update stale baseline numerics to match current `run_baseline.py` output |
| 2 | `tests/unit/test_diagnostics.py` | Add one assertion to an existing test |
| 3 & 4 | `tests/unit/test_competitor_rate_cli.py` | Add two new test functions |

---

## Task 1: Update stale baseline numerics in usage_guide.md (P0-4)

**Context:** The P2-5 capital/RWA charge was added after the usage_guide was last updated. The doc still shows pre-P2-5 numbers. The current `python3 examples/run_baseline.py` output is the ground truth; the golden fixture at `tests/fixtures/baseline_golden.json` already matches it.

**Files:**
- Modify: `docs/usage_guide.md:351-430`

- [ ] **Step 1: Verify the current baseline output**

Run:
```bash
python3 examples/run_baseline.py
```

Confirm the output contains:
```
Best joint pricing decision
  Home loan rate:          8.10%
  Term deposit rate:       4.95%
  Objective value:         $10,149,221
  Expected loan volume:    $539,597,610
  Expected deposit volume: $352,518,457
  Funding shortfall:       $0
```

And the decomposition:
```
Winner objective decomposition
  Loan interest income:     $43,707,406
  Expected credit loss:     $3,646,398
  Deposit interest cost:    $17,449,664
  Wholesale funding cost:   $10,195,814
  Funding shortfall penalty: $0
  Total objective:          $10,149,221
```

And the top 5:
```
Top 5 pricing candidates
  1. loan=8.10%, deposit=4.95%, objective=$10,149,221, loans=$539,597,610, deposits=$352,518,457, shortfall=$0
  2. loan=8.15%, deposit=4.85%, objective=$10,144,397, loans=$508,992,979, deposits=$330,238,746, shortfall=$606,690
  3. loan=8.10%, deposit=4.90%, objective=$10,098,229, loans=$539,597,610, deposits=$341,484,767, shortfall=$9,253,679
  4. loan=8.05%, deposit=5.05%, objective=$10,082,764, loans=$571,538,523, deposits=$373,823,791, shortfall=$0
  5. loan=8.05%, deposit=5.00%, objective=$10,074,894, loans=$571,538,523, deposits=$363,307,787, shortfall=$8,192,253
```

Expected: output matches the numbers above. If any number differs, stop and investigate before proceeding.

- [ ] **Step 2: Replace the Best Pricing Decision code block**

In `docs/usage_guide.md`, find and replace the entire fenced block starting at approximately line 351:

Old:
```text
Best joint pricing decision
  Home loan rate:          8.00%
  Term deposit rate:       5.15%
  Objective value:         $12,496,386
  Expected loan volume:    $604,819,814
  Expected deposit volume: $393,936,646
  Funding shortfall:       $0
```

New:
```text
Best joint pricing decision
  Home loan rate:          8.10%
  Term deposit rate:       4.95%
  Objective value:         $10,149,221
  Expected loan volume:    $539,597,610
  Expected deposit volume: $352,518,457
  Funding shortfall:       $0
```

- [ ] **Step 3: Update the Best Pricing Decision prose**

Find the paragraph immediately after the code block (starting "Plain-English interpretation:"). Replace each stale number inline:

- `8.00%` home loan rate → `8.10%`
- `5.15%` term deposit rate → `4.95%`
- `$12.5M` (or `$12,496,386`) → `$10.1M`
- `$605M` (or `$604,819,814`) → `$540M`
- `$394M` (or `$393,936,646`) → `$353M`

The result should read:

```markdown
- The best tested pair is an `8.10%` home loan rate and a `4.95%` term deposit rate.
- The expected economic value of that decision is about `$10.1M`.
- The model expects about `$540M` of accepted home loans.
- The model expects about `$353M` of term deposits raised.
- There is no final funding shortfall after the adaptive boundary follow-up.
```

- [ ] **Step 4: Replace the Objective Decomposition code block and prose**

Find and replace the decomposition fenced block:

Old:
```text
Loan interest income:      $48,385,585
Expected credit loss:      $4,108,330
Deposit interest cost:     $20,287,737
Wholesale funding cost:    $11,493,133
Funding shortfall penalty: $0
Total objective:           $12,496,386
```

New:
```text
Loan interest income:      $43,707,406
Expected credit loss:      $3,646,398
Deposit interest cost:     $17,449,664
Wholesale funding cost:    $10,195,814
Funding shortfall penalty: $0
Total objective:           $10,149,221
```

Then replace the arithmetic fenced block:

Old:
```text
$48.39M loan interest income
- $4.11M expected credit loss
- $20.29M deposit interest cost
- $11.49M wholesale funding cost
- $0 funding shortfall penalty
= $12.50M total objective
```

New:
```text
$43.71M loan interest income
- $3.65M expected credit loss
- $17.45M deposit interest cost
- $10.20M wholesale funding cost
- $0 funding shortfall penalty
= $10.15M total objective (after capital/RWA charge of ~$2.27M, not shown separately)
```

Then update the prose that follows (find "the `8.00%` home loan rate" and change to `8.10%`).

- [ ] **Step 5: Replace the Funding Shortfall code block and prose**

Find and replace the funding shortfall fenced block:

Old:
```text
Expected loan volume:    about $604.8M
Required deposit funding: about $393.1M
Expected deposit volume: about $393.9M
Funding shortfall:       $0
```

New:
```text
Expected loan volume:     about $539.6M
Required deposit funding: about $350.7M
Expected deposit volume:  about $352.5M
Funding shortfall:        $0
```

- [ ] **Step 6: Update the Top 5 Candidates interpretation prose**

Find the block beginning "Current interpretation:" under "### Top 5 Candidates". Replace the four bullet points:

Old:
```markdown
- Rank 1, `8.00%` loan and `5.15%` deposit, is best overall.
- Rank 2, `8.00%` loan and `5.10%` deposit, has a slightly lower deposit cost but leaves a funding
  shortfall.
- Rank 3, `8.05%` loan and `5.05%` deposit, has a higher loan rate but lower expected loan volume.
- Rank 4, `8.05%` loan and `5.00%` deposit, leaves a smaller funding shortfall but lower objective.
- Rank 5 is close, but still worse than the winner on the current objective.
```

New:
```markdown
- Rank 1, `8.10%` loan and `4.95%` deposit, is best overall.
- Rank 2, `8.15%` loan and `4.85%` deposit, has a higher loan rate but lower deposit volume and a
  small funding shortfall ($607K).
- Rank 3, `8.10%` loan and `4.90%` deposit, matches rank 1 on loans but raises fewer deposits,
  leaving a funding shortfall ($9.3M) that slightly reduces the objective.
- Rank 4, `8.05%` loan and `5.05%` deposit, has a higher loan volume and no shortfall but a lower
  loan rate reduces margin.
- Rank 5 is close, but still worse than the winner on the current objective.
```

- [ ] **Step 7: Verify the guide is internally consistent**

Scan `docs/usage_guide.md` for any remaining occurrences of the old numbers:

```bash
grep -n "8\.00%\|5\.15%\|12,496,386\|12\.5M\|604,819,814\|605M\|393,936,646\|394M\|393\.9M\|393\.1M\|604\.8M\|48,385,585\|48\.39M\|4,108,330\|4\.11M\|20,287,737\|20\.29M\|11,493,133\|11\.49M" docs/usage_guide.md
```

Expected: no matches. Fix any remaining occurrences.

- [ ] **Step 8: Commit**

```bash
git add docs/usage_guide.md
git commit -m "docs: update usage_guide baseline numerics after P2-5 capital charge

The P2-5 capital/RWA charge merge changed the optimizer objective from
~$12.5M to ~$10.1M and shifted the optimal rates. All result boxes, prose,
and Top 5 interpretation updated to match current run_baseline.py output."
```

---

## Task 2: Add post-expansion boundary assertion (P1-2)

**Context:** `test_baseline_diagnostics_runs_boundary_follow_up_before_accepting_result` verifies that the initial grid hits a boundary and that the grid expands. It does not assert that the final (post-expansion) result is free of boundary warnings. A regression that leaves the warning set after expansion would silently pass this test. The actual baseline run shows "No boundary warnings." after expansion, so the assertion is valid and will pass immediately.

**Files:**
- Modify: `tests/unit/test_diagnostics.py:206-215`

- [ ] **Step 1: Locate the test**

Open `tests/unit/test_diagnostics.py`. Find `test_baseline_diagnostics_runs_boundary_follow_up_before_accepting_result` (currently around line 206). It should end at approximately:

```python
    assert diagnostics.grid_results["loan_rate"].max() > max(initial_loan_grid)
    assert diagnostics.grid_results["deposit_rate"].max() > max(initial_deposit_grid)
```

- [ ] **Step 2: Add the post-expansion assertion**

Add one line immediately after the two grid-size assertions:

```python
    assert diagnostics.grid_results["loan_rate"].max() > max(initial_loan_grid)
    assert diagnostics.grid_results["deposit_rate"].max() > max(initial_deposit_grid)
    assert not diagnostics.boundary_diagnostics.has_warning
```

The complete test should now look like:

```python
def test_baseline_diagnostics_runs_boundary_follow_up_before_accepting_result():
    config = load_config("configs/base.yaml")
    initial_loan_grid, initial_deposit_grid = build_configured_rate_grids(config)

    diagnostics = run_baseline_diagnostics(config, max_boundary_expansions=1)

    assert diagnostics.initial_boundary_diagnostics.has_warning
    assert diagnostics.expansion_rounds == 1
    assert diagnostics.grid_results["loan_rate"].max() > max(initial_loan_grid)
    assert diagnostics.grid_results["deposit_rate"].max() > max(initial_deposit_grid)
    assert not diagnostics.boundary_diagnostics.has_warning
```

- [ ] **Step 3: Run just this test to confirm it passes**

```bash
python3 -m pytest tests/unit/test_diagnostics.py::test_baseline_diagnostics_runs_boundary_follow_up_before_accepting_result -v
```

Expected: `PASSED`. If it fails with `has_warning is True`, stop — the baseline grid expansion no longer resolves the boundary warning, and you must investigate before committing.

- [ ] **Step 4: Run the full test suite to confirm no regressions**

```bash
python3 -m pytest -q
```

Expected: all tests pass (≥301 passed, 0 failed).

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_diagnostics.py
git commit -m "test: assert boundary warning resolved after adaptive expansion

Ensures that a regression where expansion leaves the boundary_warning_flag
set would be caught. The baseline grid expansion is expected to clear the
warning, so this assertion should always hold."
```

---

## Task 3: CLI test for --max-staleness-hours (P1-4 part 1)

**Context:** The `--max-staleness-hours` flag was added to the `validate` subcommand at `src/balance_pricing_engine/competitor_rates/cli.py:79-83` and is wired to `validate_staleness` at `cli.py:157-162`. No test exercises this flag. The existing fixture at `tests/fixtures/competitor_rates/latest.json` has `captured_at_utc: "2026-04-27T00:00:00Z"`, which is ~24 days old and will trigger staleness with `--max-staleness-hours 0`. With `--strict true` (the `validate` default), `validate_staleness` returns severity `"error"`, which sets `failed=True` and returns exit code 1.

**Files:**
- Modify: `tests/unit/test_competitor_rate_cli.py`

- [ ] **Step 1: Add the staleness test to the test file**

Open `tests/unit/test_competitor_rate_cli.py`. Add the following test function after the existing `test_validate_command_reports_fixture_status` test:

```python
def test_validate_command_returns_nonzero_when_file_exceeds_staleness_limit(capsys):
    # FIXTURE captured_at_utc is 2026-04-27, always older than 0 hours.
    exit_code = cli.main(["validate", "--source-file", FIXTURE, "--max-staleness-hours", "0"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "stale" in captured.out.lower()
```

- [ ] **Step 2: Run just this test to confirm it passes**

```bash
python3 -m pytest tests/unit/test_competitor_rate_cli.py::test_validate_command_returns_nonzero_when_file_exceeds_staleness_limit -v
```

Expected: `PASSED`. If it fails:
- Exit code 0 when 1 expected → check that `validate_staleness` is being called and `strict=True` (default) makes it an error-severity issue.
- `"stale"` not in output → check `_validate` in `cli.py:159` that it prints each staleness issue.

- [ ] **Step 3: Run the full test suite**

```bash
python3 -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_competitor_rate_cli.py
git commit -m "test: cover --max-staleness-hours flag in validate subcommand

The flag was wired up but untested. Verifies that a file older than the
specified limit causes a non-zero exit code and prints a staleness message."
```

---

## Task 4: CLI test for --previous-file (P1-4 part 2)

**Context:** The `--previous-file` flag was added to the `validate` subcommand at `cli.py:84-88` and wired to `validate_rate_run(run, previous_run=..., strict=...)` at `cli.py:153-154`. `validate_rate_run` calls `_add_movement_issues` when `previous_run` is provided, which emits `"warning"` severity issues for movements > `LARGE_MOVEMENT_PCT` (0.50 percentage points). Two tests are needed: one that confirms the flag is accepted and wired (no crash, correct exit when rates are identical), and one that confirms movement warnings appear in output when rates differ by more than 0.50 pp.

**Files:**
- Modify: `tests/unit/test_competitor_rate_cli.py`

- [ ] **Step 1: Add the no-movement test (same file as current and previous)**

Add the following test after the staleness test from Task 3:

```python
def test_validate_command_accepts_previous_file_with_no_movement(capsys):
    # Passing the same file as current and previous means zero rate movement.
    exit_code = cli.main([
        "validate",
        "--source-file", FIXTURE,
        "--previous-file", FIXTURE,
    ])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Validation status: valid" in captured.out
```

- [ ] **Step 2: Run just this test to confirm it passes**

```bash
python3 -m pytest tests/unit/test_competitor_rate_cli.py::test_validate_command_accepts_previous_file_with_no_movement -v
```

Expected: `PASSED`.

- [ ] **Step 3: Add the large-movement test (fixture with modified rates)**

Add the following test. It creates a mutated copy of the fixture with the ANZ home loan rate changed by +1.0 percentage points (well above the 0.50 pp threshold), writes it to a temp file, and checks that the movement warning appears in output. Note: large movements produce `"warning"` severity (not `"error"`), so exit code remains 0.

```python
def test_validate_command_reports_large_rate_movement_from_previous_file(tmp_path, capsys):
    # Load the fixture and build a "previous" run with ANZ home loan at rate - 1.0 pp.
    previous_run = load_rate_run(FIXTURE)
    modified_records = []
    for record in previous_run.records:
        if record.bank == "ANZ" and str(record.product_type) == "home_loan":
            modified_records.append(
                record.model_copy(
                    update={"advertised_rate_pct": record.advertised_rate_pct - 1.0}
                )
            )
        else:
            modified_records.append(record)
    previous_with_low_anz = previous_run.model_copy(update={"records": modified_records})
    previous_file = tmp_path / "previous.json"
    previous_file.write_text(previous_with_low_anz.model_dump_json(), encoding="utf-8")

    # Current file (FIXTURE) has ANZ home loan 1.0 pp higher → large movement.
    exit_code = cli.main([
        "validate",
        "--source-file", FIXTURE,
        "--previous-file", str(previous_file),
    ])

    captured = capsys.readouterr()
    # Movement severity is "warning", not "error", so exit code is still 0.
    assert exit_code == 0
    assert "Large day-over-day movement" in captured.out
```

- [ ] **Step 4: Run just this test to confirm it passes**

```bash
python3 -m pytest tests/unit/test_competitor_rate_cli.py::test_validate_command_reports_large_rate_movement_from_previous_file -v
```

Expected: `PASSED`. If it fails with `"Large day-over-day movement" not in output`:
- Check that `_print_validation_summary` in `cli.py` prints each issue from the summary.
- Check that `_add_movement_issues` in `validate.py` is being reached (requires `previous_run is not None`).

- [ ] **Step 5: Run the full test suite and ruff**

```bash
python3 -m pytest -q
python3 -m ruff check .
```

Expected: all tests pass, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add tests/unit/test_competitor_rate_cli.py
git commit -m "test: cover --previous-file flag in validate subcommand

Tests both the no-movement (same-file) case and the large-movement (>0.50 pp)
case. The flag was wired up at cli.py:153-154 but untested."
```

---

## Self-Review

**Spec coverage check:**

| Finding | Task | Covered? |
|---------|------|----------|
| P0-4: usage_guide baseline numerics stale | Task 1 | Yes — all four stale blocks updated |
| P1-2: no post-expansion boundary_warning_flag=False assertion | Task 2 | Yes — one assertion added |
| P1-4: --max-staleness-hours untested | Task 3 | Yes — staleness test added |
| P1-4: --previous-file untested | Task 4 | Yes — no-movement + large-movement tests added |

**Placeholder scan:** No TBD, TODO, or vague instructions present. Every step has exact code, exact commands, and expected output.

**Type consistency:** `diagnostics.boundary_diagnostics.has_warning` used in Task 2 matches the `BoundaryDiagnostics` field confirmed at `scenarios/runner.py:151`. `load_rate_run` used in Task 4 is already imported in `test_competitor_rate_cli.py:10`. `record.model_copy` is standard Pydantic v2 (used by existing tests in the same file at line 169).
