"""Run the portable TEST-002 regression anchors against pre-fix revisions (Phase 3c).

For each revision this script creates a disposable, detached ``git worktree`` in a temporary
directory, copies in *unmodified* ``tests/unit/economic_strategies.py`` and
``tests/unit/test_invariants_historical_anchors.py`` from the current checkout, runs the
anchors with that revision's own ``src/`` first on ``sys.path``, records each outcome, and
removes the worktree. Nothing is committed, and no branch is created or changed.

Usage (from the repository root, with the project virtualenv)::

    python docs/audits/2026-11-21-phase-3c-evidence/phase_3c_historical.py \
        --output docs/audits/2026-11-21-phase-3c-evidence/historical_results.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
# Local pytest-generated JUnit XML only (trusted input); no external XML is parsed.
import xml.etree.ElementTree as ET  # noqa: S405
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ANCHOR_FILES = (
    "tests/unit/economic_strategies.py",
    "tests/unit/test_invariants_historical_anchors.py",
)
REVISIONS = (
    # label, revision, the defect each anchor is expected to expose there
    ("audit_62b81d2", "62b81d2", "OPT-001 baseline (November audit SHA)"),
    ("pre_3a_3304b20", "3304b20", "OPT-002 baseline (master before PR #78)"),
    ("pre_3b_5141833", "5141833", "FIN-002 baseline (master before PR #79)"),
    ("current_HEAD", "HEAD", "current branch (all anchors must pass)"),
)
EXPECTED_FAILURE = {
    "audit_62b81d2": (
        "test_a1_opt001_roe_floor_is_met_or_its_relaxation_is_disclosed",
        "OPT-001 undisclosed relaxation",
    ),
    "pre_3a_3304b20": (
        "test_a2_opt002_single_tier_segmented_equals_joint_grid_search",
        "OPT-002 segmented/joint mismatch",
    ),
    "pre_3b_5141833": (
        "test_a3_fin002_unrun_back_book_doubles_the_period_zero_contribution",
        "FIN-002 back-book mismatch",
    ),
}
ANCHORS = {test for test, _ in EXPECTED_FAILURE.values()}


def _run(cmd: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _outcomes(junit: Path) -> dict[str, dict]:
    results = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        failure = case.find("failure")
        error = case.find("error")
        problem = failure if failure is not None else error
        status = "passed"
        if problem is not None:
            status = "failed" if failure is not None else "error"
        elif case.find("skipped") is not None:
            status = "skipped"
        message, falsifying = "", []
        if problem is not None:
            message = (problem.get("message") or "").partition("\n")[0][:300]
            text = (problem.text or "").splitlines()
            # The first assertion message carrying the economic evidence, and the shrunk
            # Hypothesis example that produced it.
            falsifying = [line.strip()[:300] for line in text
                          if line.lstrip().startswith(("AssertionError", "E   Falsifying",
                                                        "E   AssertionError"))][:4]
            falsifying += [line.strip()[:300] for line in text if "Falsifying example" in line][:2]
        results[case.get("name")] = {
            "status": status, "message": message, "evidence": falsifying
        }
    return results


def run_revision(label: str, revision: str, workdir: Path) -> dict:
    sha = _run(["git", "rev-parse", "--short", revision], REPO).stdout.strip()
    tree = workdir / label
    added = _run(["git", "worktree", "add", "--detach", str(tree), revision], REPO)
    if added.returncode != 0:
        return {"revision": sha, "error": added.stderr.strip()}
    try:
        for name in ANCHOR_FILES:
            shutil.copy2(REPO / name, tree / name)
        env = {**os.environ, "PYTHONPATH": str(tree / "src")}
        imported = _run(
            [sys.executable, "-c", "import balance_pricing_engine as b; print(b.__file__)"],
            tree,
            env,
        )
        resolved = Path(imported.stdout.strip()).resolve()
        if imported.returncode != 0 or not resolved.is_relative_to((tree / "src").resolve()):
            return {"revision": sha, "error": "Could not import the checked-out package"}
        junit = workdir / f"{label}.xml"
        completed = _run(
            [sys.executable, "-m", "pytest", ANCHOR_FILES[1], "-q", "-p", "no:cacheprovider",
             f"--junitxml={junit}"],
            tree,
            env,
        )
        return {
            "revision": sha,
            "package_imported_from": str(resolved.relative_to(workdir.resolve())),
            "pytest_exit_code": completed.returncode,
            "tests": _outcomes(junit) if junit.exists() else {},
            "stdout_tail": completed.stdout.splitlines()[-3:],
        }
    finally:
        removed = _run(["git", "worktree", "remove", "--force", str(tree)], REPO)
        removed.check_returncode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report: dict = {"python": sys.version.split()[0], "revisions": {}}
    with tempfile.TemporaryDirectory(prefix="phase3c-historical-") as tmp:
        workdir = Path(tmp)
        for label, revision, purpose in REVISIONS:
            result = run_revision(label, revision, workdir)
            result["purpose"] = purpose
            tests = result.get("tests", {})
            complete = set(tests) == ANCHORS and all(
                t["status"] in {"passed", "failed"} for t in tests.values()
            )
            if label in EXPECTED_FAILURE:
                anchor, assertion = EXPECTED_FAILURE[label]
                outcome = tests.get(anchor, {})
                result["expected_failing_anchor"] = anchor
                result["expected_assertion"] = assertion
                assertion_prefix = f"AssertionError: {assertion}"
                result["defect_detected"] = (
                    complete
                    and result["pytest_exit_code"] == 1
                    and outcome["status"] == "failed"
                    and (
                        outcome["message"].startswith(assertion_prefix)
                        or any(assertion_prefix in line for line in outcome["evidence"])
                    )
                )
            else:
                result["all_anchors_pass"] = (
                    complete
                    and result["pytest_exit_code"] == 0
                    and all(t["status"] == "passed" for t in tests.values())
                )
            report["revisions"][label] = result
            print(label, json.dumps(result, indent=2))
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    detected = all(r.get("defect_detected") for k, r in report["revisions"].items()
                   if k in EXPECTED_FAILURE)
    current_ok = report["revisions"]["current_HEAD"].get("all_anchors_pass", False)
    return 0 if detected and current_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
