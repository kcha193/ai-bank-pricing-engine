# Pricing Run Explainer + Experiment Ledger — Implementation Plan

> **STATUS: ✅ COMPLETE** — run-ledger governance layer — PR #21

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic, fully offline `run_ledger` layer that turns every pricing run into an auditable decision-support package: a unique timestamped directory containing run metadata, a config snapshot, existing CSV/JSON artifacts, a markdown pricing memo, and a governance checklist.

**Architecture:** A new `src/balance_pricing_engine/run_ledger/` subpackage with five focused modules (`directory`, `metadata`, `config_snapshot`, `memo`, `checklist`) orchestrated by a single `write_run_ledger()` entry point. The layer wraps the existing `ScenarioDiagnostics.write_artifacts()` call and adds the new artifacts on top. No external dependencies added.

**Tech Stack:** Python stdlib only (hashlib, json, shutil, subprocess, uuid, importlib.metadata) + existing project types (EngineConfig, ScenarioDiagnostics, OptimizationResult, ObjectiveDecomposition, BoundaryDiagnostics, pandas). Tests use pytest + tmp_path fixtures.

---

## File Map

| Action | Path | Responsibility |
|--------|------|---------------|
| Create | `src/balance_pricing_engine/run_ledger/__init__.py` | Re-exports `write_run_ledger` |
| Create | `src/balance_pricing_engine/run_ledger/directory.py` | `create_run_directory()` — timestamped path creation |
| Create | `src/balance_pricing_engine/run_ledger/metadata.py` | `RunMetadata` dataclass + `build_run_metadata()` + `write_run_metadata()` |
| Create | `src/balance_pricing_engine/run_ledger/config_snapshot.py` | `write_config_snapshot()` — YAML file copy |
| Create | `src/balance_pricing_engine/run_ledger/memo.py` | `write_pricing_memo()` — markdown memo generator |
| Create | `src/balance_pricing_engine/run_ledger/checklist.py` | `write_governance_checklist()` — markdown checklist generator |
| Create | `src/balance_pricing_engine/run_ledger/writer.py` | `write_run_ledger()` — orchestrator |
| Create | `tests/unit/test_run_ledger.py` | All tests for the new layer |
| Modify | `examples/run_baseline.py` | Add `write_run_ledger()` call at end of `main()` |

---

## Task 1: `directory.py` — Timestamped Run Directory

**Files:**
- Create: `src/balance_pricing_engine/run_ledger/directory.py`
- Test: `tests/unit/test_run_ledger.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_run_ledger.py
from __future__ import annotations

from pathlib import Path

import pytest

from balance_pricing_engine.run_ledger.directory import create_run_directory


def test_create_run_directory_is_created(tmp_path: Path) -> None:
    run_dir = create_run_directory("baseline", base_dir=tmp_path)
    assert run_dir.exists()
    assert run_dir.is_dir()


def test_create_run_directory_name_contains_scenario(tmp_path: Path) -> None:
    run_dir = create_run_directory("my_scenario", base_dir=tmp_path)
    assert "my_scenario" in run_dir.name


def test_create_run_directory_name_has_timestamp_prefix(tmp_path: Path) -> None:
    run_dir = create_run_directory("baseline", base_dir=tmp_path)
    # Expected format: YYYYMMDD_HHMMSS_baseline
    date_part = run_dir.name.split("_")[0]
    assert len(date_part) == 8 and date_part.isdigit()


def test_create_run_directory_spaces_replaced(tmp_path: Path) -> None:
    run_dir = create_run_directory("my scenario", base_dir=tmp_path)
    assert " " not in run_dir.name


def test_create_run_directory_default_base_dir_is_outputs_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_dir = create_run_directory("baseline")
    assert run_dir.parent == tmp_path / "outputs" / "runs"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/kevinchang/Documents/ai-bank-pricing-engine
python3 -m pytest tests/unit/test_run_ledger.py -v
```

Expected: `ModuleNotFoundError: No module named 'balance_pricing_engine.run_ledger'`

- [ ] **Step 3: Implement `directory.py`**

```python
# src/balance_pricing_engine/run_ledger/directory.py
"""Run directory creation for the pricing run ledger."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def create_run_directory(
    scenario_name: str,
    base_dir: str | Path = "outputs/runs",
) -> Path:
    """Create and return a unique timestamped run directory.

    Directory name: {YYYYMMDD_HHMMSS}_{safe_scenario_name}
    """
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_name = scenario_name.replace(" ", "_").replace("/", "_")
    run_dir = Path(base_dir) / f"{timestamp}_{safe_name}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir
```

Also create the package stub (will be filled in Task 6):

```python
# src/balance_pricing_engine/run_ledger/__init__.py
"""Pricing run explainer and experiment ledger."""
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/unit/test_run_ledger.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/run_ledger/__init__.py \
        src/balance_pricing_engine/run_ledger/directory.py \
        tests/unit/test_run_ledger.py
git commit -m "feat: add run_ledger.directory for timestamped run directories"
```

---

## Task 2: `metadata.py` — Run Metadata

**Files:**
- Create: `src/balance_pricing_engine/run_ledger/metadata.py`
- Modify: `tests/unit/test_run_ledger.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_run_ledger.py`:

```python
import json

from balance_pricing_engine.config import load_config
from balance_pricing_engine.run_ledger.metadata import (
    RunMetadata,
    build_run_metadata,
    write_run_metadata,
)
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics


@pytest.fixture(scope="module")
def config():
    return load_config("configs/base.yaml")


@pytest.fixture(scope="module")
def diagnostics(config):
    return run_baseline_diagnostics(config)


REQUIRED_METADATA_FIELDS = {
    "run_id",
    "scenario_name",
    "timestamp",
    "config_path",
    "config_hash",
    "python_version",
    "boundary_warning_flag",
    "initial_boundary_warning_flag",
    "expansion_rounds",
    "policy_band_breached",
}


def test_build_run_metadata_has_required_fields(diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    from dataclasses import asdict
    d = asdict(metadata)
    assert REQUIRED_METADATA_FIELDS.issubset(d.keys())


def test_build_run_metadata_scenario_name(diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    assert metadata.scenario_name == "baseline"


def test_build_run_metadata_config_path(diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    assert metadata.config_path == "configs/base.yaml"


def test_build_run_metadata_config_hash_is_stable(diagnostics) -> None:
    m1 = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    m2 = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    assert m1.config_hash == m2.config_hash
    assert len(m1.config_hash) == 64  # sha256 hex digest


def test_build_run_metadata_python_version(diagnostics) -> None:
    import sys
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    assert metadata.python_version.startswith(
        f"{sys.version_info.major}.{sys.version_info.minor}."
    )


def test_build_run_metadata_boundary_flags_match_diagnostics(diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    assert metadata.boundary_warning_flag == diagnostics.boundary_diagnostics.has_warning
    assert metadata.initial_boundary_warning_flag == diagnostics.initial_boundary_diagnostics.has_warning
    assert metadata.expansion_rounds == diagnostics.expansion_rounds
    assert metadata.policy_band_breached == diagnostics.policy_band_breached


def test_write_run_metadata_creates_json(tmp_path: Path, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_run_metadata(metadata, tmp_path)
    out = tmp_path / "metadata.json"
    assert out.exists()
    data = json.loads(out.read_text())
    assert "run_id" in data
    assert "config_hash" in data


def test_write_run_metadata_json_is_valid(tmp_path: Path, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_run_metadata(metadata, tmp_path)
    data = json.loads((tmp_path / "metadata.json").read_text())
    assert data["scenario_name"] == "baseline"
    assert isinstance(data["boundary_warning_flag"], bool)
    assert isinstance(data["expansion_rounds"], int)
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/unit/test_run_ledger.py -v -k "metadata"
```

Expected: `ImportError: cannot import name 'RunMetadata'`

- [ ] **Step 3: Implement `metadata.py`**

```python
# src/balance_pricing_engine/run_ledger/metadata.py
"""Run metadata builder for the pricing run ledger."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from balance_pricing_engine.scenarios.runner import ScenarioDiagnostics


def _git_commit() -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _package_version() -> Optional[str]:
    try:
        from importlib.metadata import version

        return version("ai-bank-pricing-engine")
    except Exception:
        return None


def _config_hash(config_path: str | Path) -> str:
    return hashlib.sha256(Path(config_path).read_bytes()).hexdigest()


@dataclass(frozen=True)
class RunMetadata:
    run_id: str
    scenario_name: str
    timestamp: str
    git_commit: Optional[str]
    config_path: str
    config_hash: str
    python_version: str
    package_version: Optional[str]
    boundary_warning_flag: bool
    initial_boundary_warning_flag: bool
    expansion_rounds: int
    policy_band_breached: bool
    competitor_rate_source: Optional[str]


def build_run_metadata(
    scenario_name: str,
    config_path: str | Path,
    diagnostics: "ScenarioDiagnostics",
    competitor_rate_source: Optional[str] = None,
) -> RunMetadata:
    """Build a RunMetadata record from a completed ScenarioDiagnostics."""
    vi = sys.version_info
    return RunMetadata(
        run_id=str(uuid.uuid4()),
        scenario_name=scenario_name,
        timestamp=datetime.utcnow().isoformat(),
        git_commit=_git_commit(),
        config_path=str(config_path),
        config_hash=_config_hash(config_path),
        python_version=f"{vi.major}.{vi.minor}.{vi.micro}",
        package_version=_package_version(),
        boundary_warning_flag=diagnostics.boundary_diagnostics.has_warning,
        initial_boundary_warning_flag=diagnostics.initial_boundary_diagnostics.has_warning,
        expansion_rounds=diagnostics.expansion_rounds,
        policy_band_breached=diagnostics.policy_band_breached,
        competitor_rate_source=competitor_rate_source,
    )


def write_run_metadata(metadata: RunMetadata, run_dir: Path) -> None:
    """Serialise RunMetadata to metadata.json inside run_dir."""
    (run_dir / "metadata.json").write_text(
        json.dumps(asdict(metadata), indent=2, sort_keys=True),
        encoding="utf-8",
    )
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/unit/test_run_ledger.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/run_ledger/metadata.py tests/unit/test_run_ledger.py
git commit -m "feat: add run_ledger.metadata with RunMetadata dataclass"
```

---

## Task 3: `config_snapshot.py` — Config File Copy

**Files:**
- Create: `src/balance_pricing_engine/run_ledger/config_snapshot.py`
- Modify: `tests/unit/test_run_ledger.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_run_ledger.py`:

```python
from balance_pricing_engine.run_ledger.config_snapshot import write_config_snapshot


def test_write_config_snapshot_creates_file(tmp_path: Path) -> None:
    write_config_snapshot("configs/base.yaml", tmp_path)
    assert (tmp_path / "config_snapshot.yaml").exists()


def test_write_config_snapshot_content_matches_original(tmp_path: Path) -> None:
    write_config_snapshot("configs/base.yaml", tmp_path)
    original = Path("configs/base.yaml").read_text(encoding="utf-8")
    snapshot = (tmp_path / "config_snapshot.yaml").read_text(encoding="utf-8")
    assert original == snapshot
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/unit/test_run_ledger.py -v -k "config_snapshot"
```

Expected: `ImportError: cannot import name 'write_config_snapshot'`

- [ ] **Step 3: Implement `config_snapshot.py`**

```python
# src/balance_pricing_engine/run_ledger/config_snapshot.py
"""Config snapshot writer for the pricing run ledger."""

from __future__ import annotations

import shutil
from pathlib import Path


def write_config_snapshot(config_path: str | Path, run_dir: Path) -> None:
    """Copy the config YAML into run_dir as config_snapshot.yaml."""
    shutil.copy2(config_path, run_dir / "config_snapshot.yaml")
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/unit/test_run_ledger.py -v -k "config_snapshot"
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/run_ledger/config_snapshot.py tests/unit/test_run_ledger.py
git commit -m "feat: add run_ledger.config_snapshot"
```

---

## Task 4: `memo.py` — Pricing Memo Generator

**Files:**
- Create: `src/balance_pricing_engine/run_ledger/memo.py`
- Modify: `tests/unit/test_run_ledger.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_run_ledger.py`:

```python
from balance_pricing_engine.run_ledger.memo import write_pricing_memo


def test_write_pricing_memo_creates_file(tmp_path: Path, config, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_pricing_memo(tmp_path, metadata, config, diagnostics)
    assert (tmp_path / "pricing_memo.md").exists()


def test_pricing_memo_contains_recommendation_section(tmp_path: Path, config, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_pricing_memo(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "pricing_memo.md").read_text()
    assert "## Recommendation" in content


def test_pricing_memo_contains_objective_decomposition(tmp_path: Path, config, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_pricing_memo(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "pricing_memo.md").read_text()
    assert "## Objective Decomposition" in content


def test_pricing_memo_contains_boundary_diagnostics(tmp_path: Path, config, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_pricing_memo(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "pricing_memo.md").read_text()
    assert "## Boundary Diagnostics" in content


def test_pricing_memo_contains_model_risk_notes(tmp_path: Path, config, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_pricing_memo(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "pricing_memo.md").read_text()
    assert "## Model-Risk Notes" in content


def test_pricing_memo_contains_committee_questions(tmp_path: Path, config, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_pricing_memo(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "pricing_memo.md").read_text()
    assert "## Questions for Pricing Committee" in content


def test_pricing_memo_contains_synthetic_data_limitation(
    tmp_path: Path, config, diagnostics
) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_pricing_memo(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "pricing_memo.md").read_text()
    assert "## Synthetic Data Limitation" in content


def test_pricing_memo_contains_loan_rate(tmp_path: Path, config, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_pricing_memo(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "pricing_memo.md").read_text()
    loan_rate_pct = f"{diagnostics.best_result.loan_rate:.2%}"
    assert loan_rate_pct in content


def test_pricing_memo_with_sensitivity_includes_sensitivity_section(
    tmp_path: Path, config, diagnostics
) -> None:
    from balance_pricing_engine.diagnostics import run_sensitivity_analysis
    sensitivity = run_sensitivity_analysis(config)
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_pricing_memo(tmp_path, metadata, config, diagnostics, sensitivity=sensitivity)
    content = (tmp_path / "pricing_memo.md").read_text()
    assert "## Sensitivity Summary" in content
    assert "baseline" in content  # scenario name appears in table
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/unit/test_run_ledger.py -v -k "pricing_memo"
```

Expected: `ImportError: cannot import name 'write_pricing_memo'`

- [ ] **Step 3: Implement `memo.py`**

```python
# src/balance_pricing_engine/run_ledger/memo.py
"""Pricing memo generator for the pricing run ledger."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

import pandas as pd

if TYPE_CHECKING:
    from balance_pricing_engine.config import EngineConfig
    from balance_pricing_engine.run_ledger.metadata import RunMetadata
    from balance_pricing_engine.scenarios.runner import ScenarioDiagnostics


def write_pricing_memo(
    run_dir: Path,
    metadata: "RunMetadata",
    config: "EngineConfig",
    diagnostics: "ScenarioDiagnostics",
    sensitivity: Optional[pd.DataFrame] = None,
) -> None:
    """Write pricing_memo.md into run_dir."""
    content = _build_memo(metadata, config, diagnostics, sensitivity)
    (run_dir / "pricing_memo.md").write_text(content, encoding="utf-8")


def _build_memo(
    metadata: "RunMetadata",
    config: "EngineConfig",
    diagnostics: "ScenarioDiagnostics",
    sensitivity: Optional[pd.DataFrame],
) -> str:
    sections = [
        _header(metadata),
        _recommendation(diagnostics, config),
        _objective_decomposition(diagnostics),
        _funding_position(diagnostics),
        _sensitivity_section(sensitivity),
        _boundary_section(diagnostics),
        _model_risk_notes(),
        _committee_questions(config, diagnostics),
        _synthetic_data_limitation(),
    ]
    return "\n\n".join(sections)


def _header(metadata: "RunMetadata") -> str:
    git = metadata.git_commit or "n/a"
    source = metadata.competitor_rate_source or "n/a"
    return (
        f"# Pricing Memo — {metadata.scenario_name}\n\n"
        f"Run: `{metadata.run_id}`  \n"
        f"Timestamp: {metadata.timestamp}  \n"
        f"Config hash: `{metadata.config_hash}`  \n"
        f"Git commit: {git}  \n"
        f"Competitor rate source: {source}"
    )


def _recommendation(
    diagnostics: "ScenarioDiagnostics",
    config: "EngineConfig",
) -> str:
    r = diagnostics.best_result
    ref_loan = config.market.home_loan_reference_rate
    ref_dep = config.market.term_deposit_reference_rate
    loan_spread = (
        f"{r.home_loan_spread_to_market_bps:.0f} bps" if ref_loan is not None else "n/a"
    )
    dep_spread = (
        f"{r.term_deposit_spread_to_market_bps:.0f} bps" if ref_dep is not None else "n/a"
    )
    return (
        "## Recommendation\n\n"
        "| Rate | Value |\n"
        "|------|-------|\n"
        f"| Home loan rate | {r.loan_rate:.2%} |\n"
        f"| Term deposit rate | {r.deposit_rate:.2%} |\n"
        f"| Home loan spread to market | {loan_spread} |\n"
        f"| Term deposit spread to market | {dep_spread} |\n"
        f"| Objective value | ${r.total_objective:,.0f} |"
    )


def _objective_decomposition(diagnostics: "ScenarioDiagnostics") -> str:
    d = diagnostics.winner_decomposition()
    return (
        "## Objective Decomposition\n\n"
        "| Component | Amount |\n"
        "|-----------|--------|\n"
        f"| Loan interest income | ${d.loan_interest_income:,.0f} |\n"
        f"| Expected credit loss | (${d.expected_credit_loss:,.0f}) |\n"
        f"| Loan income after losses | ${d.loan_income_after_losses:,.0f} |\n"
        f"| Deposit interest cost | (${d.deposit_interest_cost:,.0f}) |\n"
        f"| Wholesale funding cost | (${d.wholesale_funding_cost:,.0f}) |\n"
        f"| Excess deposit income | ${d.excess_deposit_income:,.0f} |\n"
        f"| Funding shortfall penalty | (${d.funding_shortfall_penalty:,.0f}) |\n"
        f"| **Total objective** | **${d.total_objective:,.0f}** |"
    )


def _funding_position(diagnostics: "ScenarioDiagnostics") -> str:
    r = diagnostics.best_result
    return (
        "## Expected Volumes and Funding Position\n\n"
        "| Item | Amount |\n"
        "|------|--------|\n"
        f"| Expected loan volume | ${r.expected_loan_volume:,.0f} |\n"
        f"| Expected deposit volume | ${r.expected_deposit_volume:,.0f} |\n"
        f"| Funding shortfall | ${r.funding_shortfall:,.0f} |"
    )


def _sensitivity_section(sensitivity: Optional[pd.DataFrame]) -> str:
    if sensitivity is None or sensitivity.empty:
        return "## Sensitivity Summary\n\nSensitivity analysis not provided for this run."
    cols = [
        "scenario_name",
        "best_loan_rate",
        "best_deposit_rate",
        "objective_value",
        "delta_objective_pct",
        "boundary_warning_flag",
    ]
    available = [c for c in cols if c in sensitivity.columns]
    sub = sensitivity[available].copy()
    if "best_loan_rate" in sub:
        sub["best_loan_rate"] = sub["best_loan_rate"].map("{:.2%}".format)
    if "best_deposit_rate" in sub:
        sub["best_deposit_rate"] = sub["best_deposit_rate"].map("{:.2%}".format)
    if "objective_value" in sub:
        sub["objective_value"] = sub["objective_value"].map("${:,.0f}".format)
    if "delta_objective_pct" in sub:
        sub["delta_objective_pct"] = sub["delta_objective_pct"].map("{:+.1%}".format)
    table = sub.to_markdown(index=False)
    return f"## Sensitivity Summary\n\n{table}"


def _boundary_section(diagnostics: "ScenarioDiagnostics") -> str:
    boundary = diagnostics.boundary_diagnostics
    initial = diagnostics.initial_boundary_diagnostics
    if boundary.has_warning:
        warnings_text = "\n".join(f"- WARNING: {w}" for w in boundary.warnings)
    else:
        warnings_text = "No boundary warnings — result is interior to searched grid."
    return (
        "## Boundary Diagnostics\n\n"
        f"{warnings_text}\n\n"
        f"- Initial boundary warning: {'Yes' if initial.has_warning else 'No'}\n"
        f"- Expansion rounds completed: {diagnostics.expansion_rounds}\n"
        f"- Post-expansion boundary warning: {'Yes' if boundary.has_warning else 'No'}\n"
        f"- Policy band breached: {'Yes' if diagnostics.policy_band_breached else 'No'}"
    )


def _model_risk_notes() -> str:
    return (
        "## Model-Risk Notes\n\n"
        "- Response functions are deterministic synthetic elasticity models. "
        "No calibration to empirical data has been performed.\n"
        "- ML diagnostic models train on synthetic outcomes from the same response functions "
        "used in optimization (self-consistency bias). They do not validate real customer behavior.\n"
        "- Elasticity parameters are point estimates with no confidence bounds or priors.\n"
        "- Wholesale funding cost and funding shortfall penalty are intentionally additive in this "
        "model (see `docs/model_governance.md` for the canonical funding economics assumption).\n"
        "- Portfolio-level pricing does not account for customer-level fairness or conduct obligations."
    )


def _committee_questions(config: "EngineConfig", diagnostics: "ScenarioDiagnostics") -> str:
    r = config.response
    m = config.market
    bs = config.balance_sheet
    boundary_q = (
        "The final result still sits on a grid boundary after adaptive expansion. "
        "Has the grid range, elasticity, and funding penalty been reviewed?"
        if diagnostics.boundary_diagnostics.has_warning
        else "Boundary checks passed — confirm the searched grid range is policy-appropriate."
    )
    return (
        "## Questions for Pricing Committee\n\n"
        f"1. Are the elasticity assumptions (loan rate: {r.loan_rate_elasticity:.0f}, "
        f"deposit rate: {r.deposit_rate_elasticity:.0f}, "
        f"loan competitor gap: {r.loan_competitor_gap_elasticity:.0f}, "
        f"deposit competitor gap: {r.deposit_competitor_gap_elasticity:.0f}) "
        "aligned with current market intelligence?\n"
        f"2. Is the wholesale funding spread of {m.wholesale_funding_spread:.2%} current?\n"
        f"3. Are the competitor rates (loan: {m.competitor_loan_rate:.2%}, "
        f"deposit: {m.competitor_deposit_rate:.2%}) up to date?\n"
        f"4. {boundary_q}\n"
        f"5. Are the funding assumptions (required ratio: {bs.required_deposit_funding_ratio:.0%}, "
        f"penalty rate: {bs.funding_shortfall_penalty_rate:.2%}) aligned with current risk appetite?"
    )


def _synthetic_data_limitation() -> str:
    return (
        "## Synthetic Data Limitation\n\n"
        "All customer populations and response outcomes are generated from synthetic assumptions. "
        "This engine is a decision-support prototype. Results must not be treated as production "
        "pricing decisions without governed calibration to empirical data, regulatory review, "
        "and human approval."
    )
```

Note: `_sensitivity_section` uses `pd.DataFrame.to_markdown()`. This requires the `tabulate` package. If `tabulate` is not installed, fall back to a CSV-style list. Check first:

```bash
python3 -c "import tabulate" 2>&1
```

If unavailable, replace `sub.to_markdown(index=False)` with:
```python
lines = ["| " + " | ".join(sub.columns) + " |",
         "| " + " | ".join(["---"] * len(sub.columns)) + " |"]
for _, row in sub.iterrows():
    lines.append("| " + " | ".join(str(v) for v in row) + " |")
table = "\n".join(lines)
```

Use the fallback approach (no `tabulate` dependency) — replace `_sensitivity_section` accordingly in the implementation.

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/unit/test_run_ledger.py -v -k "pricing_memo"
```

Expected: all pricing_memo tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/run_ledger/memo.py tests/unit/test_run_ledger.py
git commit -m "feat: add run_ledger.memo pricing memo generator"
```

---

## Task 5: `checklist.py` — Governance Checklist

**Files:**
- Create: `src/balance_pricing_engine/run_ledger/checklist.py`
- Modify: `tests/unit/test_run_ledger.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_run_ledger.py`:

```python
from balance_pricing_engine.run_ledger.checklist import write_governance_checklist

REQUIRED_CHECKLIST_ITEMS = [
    "Boundary result reviewed",
    "Sensitivity reviewed",
    "Competitor-rate freshness reviewed",
    "Funding assumptions reviewed",
    "Elasticity assumptions reviewed",
    "Conduct and fairness limitations noted",
    "Human approval required",
    "Synthetic-data limitation acknowledged",
]


def test_write_governance_checklist_creates_file(tmp_path: Path, config, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_governance_checklist(tmp_path, metadata, config, diagnostics)
    assert (tmp_path / "governance_checklist.md").exists()


def test_governance_checklist_contains_all_required_checks(
    tmp_path: Path, config, diagnostics
) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_governance_checklist(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "governance_checklist.md").read_text()
    for item in REQUIRED_CHECKLIST_ITEMS:
        assert item in content, f"Missing checklist item: {item!r}"


def test_governance_checklist_uses_unchecked_boxes(tmp_path: Path, config, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_governance_checklist(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "governance_checklist.md").read_text()
    assert "- [ ]" in content


def test_governance_checklist_includes_run_id(tmp_path: Path, config, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_governance_checklist(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "governance_checklist.md").read_text()
    assert metadata.run_id in content


def test_governance_checklist_shows_boundary_status(tmp_path: Path, config, diagnostics) -> None:
    metadata = build_run_metadata("baseline", "configs/base.yaml", diagnostics)
    write_governance_checklist(tmp_path, metadata, config, diagnostics)
    content = (tmp_path / "governance_checklist.md").read_text()
    expected_status = "REQUIRES REVIEW" if metadata.boundary_warning_flag else "CLEAR"
    assert expected_status in content
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/unit/test_run_ledger.py -v -k "checklist"
```

Expected: `ImportError: cannot import name 'write_governance_checklist'`

- [ ] **Step 3: Implement `checklist.py`**

```python
# src/balance_pricing_engine/run_ledger/checklist.py
"""Governance checklist generator for the pricing run ledger."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from balance_pricing_engine.config import EngineConfig
    from balance_pricing_engine.run_ledger.metadata import RunMetadata
    from balance_pricing_engine.scenarios.runner import ScenarioDiagnostics


def write_governance_checklist(
    run_dir: Path,
    metadata: "RunMetadata",
    config: "EngineConfig",
    diagnostics: "ScenarioDiagnostics",
) -> None:
    """Write governance_checklist.md into run_dir."""
    content = _build_checklist(metadata, config, diagnostics)
    (run_dir / "governance_checklist.md").write_text(content, encoding="utf-8")


def _build_checklist(
    metadata: "RunMetadata",
    config: "EngineConfig",
    diagnostics: "ScenarioDiagnostics",
) -> str:
    boundary_status = "REQUIRES REVIEW" if metadata.boundary_warning_flag else "CLEAR"
    m = config.market
    r = config.response
    bs = config.balance_sheet
    source_note = (
        f"\n  - Source: {metadata.competitor_rate_source}"
        if metadata.competitor_rate_source
        else ""
    )
    pkg = metadata.package_version or "unknown"
    lines = [
        f"# Governance Checklist — {metadata.scenario_name}",
        "",
        f"Run: `{metadata.run_id}`  ",
        f"Timestamp: {metadata.timestamp}  ",
        f"Config hash: `{metadata.config_hash}`  ",
        "",
        "All items must be reviewed and signed off before this run's recommendation is accepted.",
        "",
        "---",
        "",
        "## Optimizer Diagnostics",
        "",
        f"- [ ] **Boundary result reviewed** — Status: {boundary_status}",
        f"  - Initial boundary warning: {'Yes' if metadata.initial_boundary_warning_flag else 'No'}",
        f"  - Expansion rounds completed: {metadata.expansion_rounds}",
        f"  - Post-expansion boundary warning: {'Yes' if metadata.boundary_warning_flag else 'No'}",
        f"  - Policy band breached: {'Yes' if metadata.policy_band_breached else 'No'}",
        "",
        "- [ ] **Sensitivity reviewed** — Confirm no single-factor scenario reverses the "
        "recommendation without explanation.",
        "",
        "---",
        "",
        "## Market Assumptions",
        "",
        "- [ ] **Competitor-rate freshness reviewed** — Verify competitor rates reflect current "
        f"published rates (loan: {m.competitor_loan_rate:.2%}, deposit: {m.competitor_deposit_rate:.2%})."
        f"{source_note}",
        "",
        "- [ ] **Funding assumptions reviewed** — Required deposit funding ratio "
        f"({bs.required_deposit_funding_ratio:.0%}) and penalty rate "
        f"({bs.funding_shortfall_penalty_rate:.2%}) reflect current risk appetite.",
        "",
        "---",
        "",
        "## Model Assumptions",
        "",
        "- [ ] **Elasticity assumptions reviewed** — Loan rate elasticity "
        f"({r.loan_rate_elasticity:.0f}), deposit rate elasticity ({r.deposit_rate_elasticity:.0f}), "
        f"loan competitor gap ({r.loan_competitor_gap_elasticity:.0f}), and deposit competitor gap "
        f"({r.deposit_competitor_gap_elasticity:.0f}) are consistent with market intelligence.",
        "",
        "- [ ] **Conduct and fairness limitations noted** — Portfolio-level pricing does not "
        "account for customer-level fairness or conduct obligations. Any move toward segmented "
        "pricing requires a separate governance review.",
        "",
        "---",
        "",
        "## Approval",
        "",
        "- [ ] **Human approval required** — This recommendation must not be implemented without "
        "explicit sign-off from an authorised pricing decision-maker.",
        "",
        "- [ ] **Synthetic-data limitation acknowledged** — All results are based on synthetic "
        "customer populations and must not be treated as production pricing decisions.",
        "",
        "---",
        "",
        f"_Generated by ai-bank-pricing-engine v{pkg} on {metadata.timestamp}_",
    ]
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python3 -m pytest tests/unit/test_run_ledger.py -v -k "checklist"
```

Expected: all checklist tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/balance_pricing_engine/run_ledger/checklist.py tests/unit/test_run_ledger.py
git commit -m "feat: add run_ledger.checklist governance checklist generator"
```

---

## Task 6: `writer.py` + `__init__.py` — Orchestrator

**Files:**
- Create: `src/balance_pricing_engine/run_ledger/writer.py`
- Modify: `src/balance_pricing_engine/run_ledger/__init__.py`
- Modify: `tests/unit/test_run_ledger.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_run_ledger.py`:

```python
from balance_pricing_engine.run_ledger import write_run_ledger


def test_write_run_ledger_creates_directory(tmp_path: Path, config, diagnostics) -> None:
    run_dir = write_run_ledger(
        scenario_name="baseline",
        config=config,
        config_path="configs/base.yaml",
        diagnostics=diagnostics,
        base_dir=tmp_path,
    )
    assert run_dir.exists() and run_dir.is_dir()


def test_write_run_ledger_creates_all_expected_files(
    tmp_path: Path, config, diagnostics
) -> None:
    run_dir = write_run_ledger(
        scenario_name="baseline",
        config=config,
        config_path="configs/base.yaml",
        diagnostics=diagnostics,
        base_dir=tmp_path,
    )
    expected = [
        "metadata.json",
        "config_snapshot.yaml",
        "grid_results.csv",
        "top_results.csv",
        "summary.json",
        "pricing_memo.md",
        "governance_checklist.md",
    ]
    for filename in expected:
        assert (run_dir / filename).exists(), f"Missing: {filename}"


def test_write_run_ledger_returns_path(tmp_path: Path, config, diagnostics) -> None:
    result = write_run_ledger(
        scenario_name="baseline",
        config=config,
        config_path="configs/base.yaml",
        diagnostics=diagnostics,
        base_dir=tmp_path,
    )
    assert isinstance(result, Path)
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/unit/test_run_ledger.py -v -k "write_run_ledger"
```

Expected: `ImportError: cannot import name 'write_run_ledger'`

- [ ] **Step 3: Implement `writer.py`**

```python
# src/balance_pricing_engine/run_ledger/writer.py
"""Orchestrator for the pricing run ledger."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

import pandas as pd

if TYPE_CHECKING:
    from balance_pricing_engine.config import EngineConfig
    from balance_pricing_engine.scenarios.runner import ScenarioDiagnostics

from balance_pricing_engine.run_ledger.checklist import write_governance_checklist
from balance_pricing_engine.run_ledger.config_snapshot import write_config_snapshot
from balance_pricing_engine.run_ledger.directory import create_run_directory
from balance_pricing_engine.run_ledger.memo import write_pricing_memo
from balance_pricing_engine.run_ledger.metadata import build_run_metadata, write_run_metadata


def write_run_ledger(
    scenario_name: str,
    config: "EngineConfig",
    config_path: str | Path,
    diagnostics: "ScenarioDiagnostics",
    sensitivity: Optional[pd.DataFrame] = None,
    competitor_rate_source: Optional[str] = None,
    base_dir: str | Path = "outputs/runs",
) -> Path:
    """Create a full run ledger directory and write all artifacts.

    Returns the path to the created run directory.
    """
    run_dir = create_run_directory(scenario_name, base_dir)
    metadata = build_run_metadata(
        scenario_name=scenario_name,
        config_path=config_path,
        diagnostics=diagnostics,
        competitor_rate_source=competitor_rate_source,
    )
    write_run_metadata(metadata, run_dir)
    write_config_snapshot(config_path, run_dir)
    diagnostics.write_artifacts(run_dir, sensitivity=sensitivity)
    write_pricing_memo(run_dir, metadata, config, diagnostics, sensitivity)
    write_governance_checklist(run_dir, metadata, config, diagnostics)
    return run_dir
```

- [ ] **Step 4: Update `__init__.py`**

```python
# src/balance_pricing_engine/run_ledger/__init__.py
"""Pricing run explainer and experiment ledger."""

from balance_pricing_engine.run_ledger.writer import write_run_ledger

__all__ = ["write_run_ledger"]
```

- [ ] **Step 5: Run all run_ledger tests**

```bash
python3 -m pytest tests/unit/test_run_ledger.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Run full test suite to check for regressions**

```bash
python3 -m pytest -q
```

Expected: all existing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add src/balance_pricing_engine/run_ledger/writer.py \
        src/balance_pricing_engine/run_ledger/__init__.py \
        tests/unit/test_run_ledger.py
git commit -m "feat: add run_ledger.writer orchestrator and __init__ re-export"
```

---

## Task 7: Update `examples/run_baseline.py`

**Files:**
- Modify: `examples/run_baseline.py`

- [ ] **Step 1: Update `main()` to write the run ledger**

Replace the existing `main()` function with:

```python
"""Run the baseline joint pricing scenario."""

from __future__ import annotations

from pathlib import Path

from balance_pricing_engine.config import load_config
from balance_pricing_engine.diagnostics import run_sensitivity_analysis
from balance_pricing_engine.run_ledger import write_run_ledger
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics


CONFIG_PATH = Path("configs/base.yaml")


def main() -> None:
    config = load_config(CONFIG_PATH)
    diagnostics = run_baseline_diagnostics(config)
    result = diagnostics.best_result

    print("Best joint pricing decision")
    print(f"  Home loan rate:          {result.loan_rate:.2%}")
    print(f"  Term deposit rate:       {result.deposit_rate:.2%}")
    print(f"  Objective value:         ${result.objective_value:,.0f}")
    print(f"  Loan income after loss:  ${result.loan_income_after_losses:,.0f}")
    print(f"  Deposit cost:            ${result.deposit_cost:,.0f}")
    print(f"  Wholesale funding cost:  ${result.wholesale_funding_cost:,.0f}")
    print(f"  Funding penalty:         ${result.funding_penalty:,.0f}")
    print(f"  Expected loan volume:    ${result.expected_loan_volume:,.0f}")
    print(f"  Expected deposit volume: ${result.expected_deposit_volume:,.0f}")
    print(f"  Funding shortfall:       ${result.funding_shortfall:,.0f}")
    print()
    print("Market-Relative Pricing Recommendation")
    print(
        "  Market home loan reference rate:       "
        f"{_format_rate(config.market.home_loan_reference_rate)}"
    )
    print(f"  Recommended home loan rate:            {result.loan_rate:.2%}")
    print(
        "  Home loan spread to market:            "
        f"{_format_bps(result.home_loan_spread_to_market_bps)}"
    )
    print(
        "  Market term deposit reference rate:    "
        f"{_format_rate(config.market.term_deposit_reference_rate)}"
    )
    print(f"  Recommended term deposit rate:         {result.deposit_rate:.2%}")
    print(
        "  Term deposit spread to market:         "
        f"{_format_bps(result.term_deposit_spread_to_market_bps)}"
    )
    print()
    print("Top 5 pricing candidates")
    for rank, row in diagnostics.top_results(5).iterrows():
        print(
            f"  {rank + 1}. loan={row.loan_rate:.2%}, "
            f"deposit={row.deposit_rate:.2%}, "
            f"objective=${row.total_objective:,.0f}, "
            f"loans=${row.expected_loan_volume:,.0f}, "
            f"deposits=${row.expected_deposit_volume:,.0f}, "
            f"shortfall=${row.funding_shortfall:,.0f}"
        )
    print()
    print("Winner objective decomposition")
    decomposition = diagnostics.winner_decomposition()
    print(f"  Loan interest income:     ${decomposition.loan_interest_income:,.0f}")
    print(f"  Expected credit loss:     ${decomposition.expected_credit_loss:,.0f}")
    print(f"  Deposit interest cost:    ${decomposition.deposit_interest_cost:,.0f}")
    print(f"  Wholesale funding cost:   ${decomposition.wholesale_funding_cost:,.0f}")
    print(f"  Funding shortfall penalty: ${decomposition.funding_shortfall_penalty:,.0f}")
    print(f"  Total objective:          ${decomposition.total_objective:,.0f}")
    print()
    print("Boundary diagnostics")
    if diagnostics.boundary_diagnostics.has_warning:
        for warning in diagnostics.boundary_diagnostics.warnings:
            print(f"  WARNING: {warning}")
    else:
        print("  No boundary warnings.")
    print()
    print("Sensitivity summary")
    sensitivity = run_sensitivity_analysis(config)
    for row in sensitivity.itertuples(index=False):
        warning = "yes" if row.boundary_warning_flag else "no"
        print(
            f"  {row.scenario_name}: "
            f"loan={row.best_loan_rate:.2%}, "
            f"deposit={row.best_deposit_rate:.2%}, "
            f"objective=${row.objective_value:,.0f}, "
            f"loans=${row.expected_loan_volume:,.0f}, "
            f"deposits=${row.expected_deposit_volume:,.0f}, "
            f"shortfall=${row.funding_shortfall:,.0f}, "
            f"boundary_warning={warning}"
        )
    print()

    run_dir = write_run_ledger(
        scenario_name="baseline",
        config=config,
        config_path=CONFIG_PATH,
        diagnostics=diagnostics,
        sensitivity=sensitivity,
    )
    print(f"Run ledger written to: {run_dir}")


def _format_rate(rate: float | None) -> str:
    if rate is None:
        return "n/a"
    return f"{rate:.2%}"


def _format_bps(spread_bps: float) -> str:
    if spread_bps != spread_bps:
        return "n/a"
    return f"{spread_bps:.0f} bps"


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the example to verify end-to-end**

```bash
python3 examples/run_baseline.py
```

Expected output ends with:
```
Run ledger written to: outputs/runs/YYYYMMDD_HHMMSS_baseline
```

- [ ] **Step 3: Verify the run directory contents**

```bash
ls outputs/runs/$(ls -t outputs/runs | head -1)/
```

Expected files:
```
metadata.json
config_snapshot.yaml
grid_results.csv
top_results.csv
counterfactuals.csv
cohort_attribution.csv
sensitivity.csv
summary.json
pricing_memo.md
governance_checklist.md
```

- [ ] **Step 4: Run full test suite one final time**

```bash
python3 -m pytest -q && python3 -m ruff check .
```

Expected: all tests pass, no lint errors.

- [ ] **Step 5: Commit**

```bash
git add examples/run_baseline.py
git commit -m "feat: integrate write_run_ledger into run_baseline example"
```

---

## Self-Review

### Spec coverage

| Spec requirement | Task covering it |
|-----------------|-----------------|
| Run directory `outputs/runs/{timestamp}_{scenario_name}/` | Task 1 |
| `metadata.json` with all required fields | Task 2 |
| Config snapshot | Task 3 |
| `pricing_memo.md` with all 9 specified sections | Task 4 |
| `governance_checklist.md` with all 8 checks | Task 5 |
| Tests: directory creation, metadata fields, config hash stability | Tasks 1, 2 |
| Tests: pricing memo generation | Task 4 |
| Tests: governance checklist generation | Task 5 |
| No external LLM/API dependencies | All tasks — stdlib only |
| Orchestrator tying all together | Task 6 |
| Summary of files changed, how to run, example output path | Execution handoff below |

### Placeholder scan

- No "TBD", "TODO", "fill in", or "similar to task N" in any step.
- All code blocks contain complete, runnable code.
- All test assertions include the specific value or substring being checked.

### Type consistency

- `RunMetadata` defined in Task 2, used identically in Tasks 4, 5, 6.
- `write_pricing_memo(run_dir, metadata, config, diagnostics, sensitivity)` — same signature in Task 4 tests and implementation, and called identically in Task 6.
- `write_governance_checklist(run_dir, metadata, config, diagnostics)` — same signature in Task 5 tests and implementation, and called identically in Task 6.
- `write_run_ledger` returns `Path` — asserted in Task 6 test and returned in implementation.

---

## After Implementation

**Files changed:**

| File | Change |
|------|--------|
| `src/balance_pricing_engine/run_ledger/__init__.py` | New — re-exports `write_run_ledger` |
| `src/balance_pricing_engine/run_ledger/directory.py` | New — `create_run_directory` |
| `src/balance_pricing_engine/run_ledger/metadata.py` | New — `RunMetadata`, `build_run_metadata`, `write_run_metadata` |
| `src/balance_pricing_engine/run_ledger/config_snapshot.py` | New — `write_config_snapshot` |
| `src/balance_pricing_engine/run_ledger/memo.py` | New — `write_pricing_memo` |
| `src/balance_pricing_engine/run_ledger/checklist.py` | New — `write_governance_checklist` |
| `src/balance_pricing_engine/run_ledger/writer.py` | New — `write_run_ledger` orchestrator |
| `tests/unit/test_run_ledger.py` | New — all tests |
| `examples/run_baseline.py` | Modified — adds `write_run_ledger` call |

**How to run:**

```bash
python3 examples/run_baseline.py
# → outputs/runs/YYYYMMDD_HHMMSS_baseline/
```

**Risks and follow-up:**

- The sensitivity table in the memo uses a hand-rolled markdown table. If `tabulate` is added to dependencies in future, replace with `df.to_markdown()`.
- `_sensitivity_section` silently renders an empty-sensitivity message if the DataFrame is empty — this is intentional but reviewers should note it.
- `_git_commit()` uses `subprocess` with a 5-second timeout; in air-gapped or slow environments, this adds latency. If needed, add a `skip_git=True` option to `build_run_metadata`.
- The governance checklist is write-only (no digital signature or approval workflow). Future work: add an approval mechanism (e.g., a JSON sidecar that records approver name and timestamp, gated by a CLI command).
- `write_artifacts` in `ScenarioDiagnostics` calls `cohort_attribution()` which raises `ValueError` if customer context is not present. `run_baseline_diagnostics` always populates this context, so it is safe in the baseline example. Custom scenario runners that do not pass customer context will fail at `write_artifacts`. Document this constraint.
