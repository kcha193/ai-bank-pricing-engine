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
