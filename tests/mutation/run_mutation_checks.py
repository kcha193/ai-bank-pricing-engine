"""Expected-failure mutation checks for the economic-invariant suite (TEST-002, Phase 3c).

Runs the ``economic_invariants`` suite once unmutated (must pass) and once per mutant in
``economic_mutants.MUTANTS`` (must fail). A mutant is *killed* when all its designated
detectors fail with assertions. The report lists which tests caught it.
Separate from normal CI: it is never collected by
pytest and only runs when invoked explicitly::

    python tests/mutation/run_mutation_checks.py \
        --output docs/audits/2026-11-21-phase-3c-evidence/mutation_results.json

Exit status 0 only when the clean run passes and every mutant is killed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
# Local pytest-generated JUnit XML only (trusted input); no external XML is parsed.
import xml.etree.ElementTree as ET  # noqa: S405
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

from economic_mutants import MUTANTS  # noqa: E402

SUITE = sorted(str(p.relative_to(REPO)) for p in (REPO / "tests/unit").glob("test_invariants_*.py"))

# The tests each mutant is designed to be caught by (others may catch it too).
EXPECTED_DETECTORS = {
    "roe_floor_ignored_silently": [
        "test_g01_g02_g03_g05_selection_follows_constraint_first_ranking",
        "test_a1_opt001_roe_floor_is_met_or_its_relaxation_is_disclosed",
    ],
    "loan_search_drops_shortfall_penalty": [
        "test_d01_single_tier_segmented_equals_joint_optimiser",
        "test_s01_s02_s03_s05_exact_search_matches_independent_brute_force",
        "test_a2_opt002_single_tier_segmented_equals_joint_grid_search",
    ],
    # D01 catches this only rarely: a constant per-dollar capital cost seldom moves a
    # single-tier optimum by a whole grid step. The multi-tier brute force is the detector.
    "segmented_search_drops_capital_charge": [
        "test_s01_s02_s03_s05_exact_search_matches_independent_brute_force",
    ],
    "surviving_loans_earn_cash_rate": [
        "test_m12_identical_unrun_cohorts_double_every_term",
        "test_a3_fin002_unrun_back_book_doubles_the_period_zero_contribution",
    ],
    "back_book_ecl_omitted": [
        "test_m10_m11_m17_active_book_equals_hand_objective_on_aggregate_balances",
        "test_a3_fin002_unrun_back_book_doubles_the_period_zero_contribution",
    ],
    "portfolio_funding_penalty_omitted": [
        "test_m10_m11_m17_active_book_equals_hand_objective_on_aggregate_balances",
    ],
    "funding_summed_per_cohort": [
        "test_m10_m11_m17_active_book_equals_hand_objective_on_aggregate_balances",
    ],
    "survival_linear_not_geometric": [
        "test_m01_to_m05_m18_survival_is_geometric_bounded_and_independent",
    ],
    "policy_band_breach_reported_compliant": [
        "test_p01_to_p09_adaptive_grid_discloses_band_breach_boundary_and_cap",
        "test_p10_band_and_boundary_evidence_survive_summary_json",
    ],
    "loan_competitor_gap_sign_flipped": [
        "test_r_l02_higher_competitor_loan_rate_never_lowers_acceptance",
        "test_o01_o10_joint_grid_matches_independent_end_to_end_objective",
    ],
    "wholesale_cost_not_floored": [
        "test_f01_to_f06_funding_terms_match_hand_formulas_and_signs",
    ],
    "relaxed_roe_gate_passes": [
        "test_g04_g06_to_g12_gate_statuses_follow_the_documented_policy",
    ],
    "negative_objective_gate_passes": [
        "test_g04_g06_to_g12_gate_statuses_follow_the_documented_policy",
    ],
}


def _failed_tests(junit: Path) -> tuple[list[str], list[str]]:
    assertions = []
    other_failures = []
    for case in ET.parse(junit).getroot().iter("testcase"):
        failure = case.find("failure")
        if failure is not None and (
            (failure.get("message") or "").startswith("AssertionError")
            or "AssertionError" in (failure.text or "")
        ):
            assertions.append(case.get("name"))
        elif failure is not None or case.find("error") is not None:
            other_failures.append(case.get("name"))
    return sorted(assertions), sorted(other_failures)


def _run(mutant: str | None, workdir: Path) -> dict:
    junit = workdir / f"{mutant or 'clean'}.xml"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(HERE), str(REPO / "src")]),
        "ECONOMIC_MUTANT": mutant or "",
    }
    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *SUITE, "-q", "-p", "no:cacheprovider",
         "-p", "economic_mutants", "-m", "economic_invariants", f"--junitxml={junit}"],
        cwd=REPO, env=env, capture_output=True, text=True, check=False,
    )
    failed, other_failures = _failed_tests(junit) if junit.exists() else ([], [])
    return {
        "exit_code": completed.returncode,
        "failed_tests": failed,
        "other_failures": other_failures,
        "seconds": round(time.perf_counter() - started, 1),
        "summary": completed.stdout.strip().splitlines()[-1:] if completed.stdout else [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", nargs="+", choices=MUTANTS, help="subset of mutant names")
    args = parser.parse_args()
    names = args.only or list(MUTANTS)
    report: dict = {"suite": SUITE, "mutants": {}}
    with tempfile.TemporaryDirectory(prefix="phase3c-mutants-") as tmp:
        workdir = Path(tmp)
        clean = _run(None, workdir)
        report["clean"] = clean
        print("clean", clean["exit_code"], clean["summary"])
        for name in names:
            result = _run(name, workdir)
            expected = EXPECTED_DETECTORS[name]
            result["expected_detectors"] = expected
            result["expected_detectors_missed"] = sorted(set(expected) - set(result["failed_tests"]))
            result["killed"] = (
                result["exit_code"] == 1
                and not result["expected_detectors_missed"]
            )
            report["mutants"][name] = result
            print(name, "KILLED" if result["killed"] else "SURVIVED",
                  len(result["failed_tests"]), result["expected_detectors_missed"])
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    ok = clean["exit_code"] == 0 and all(r["killed"] for r in report["mutants"].values())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
