"""Run metadata builder for the pricing run ledger."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from balance_pricing_engine.config_identity import effective_config_hash, file_sha256_if_present

if TYPE_CHECKING:
    from balance_pricing_engine.config import EngineConfig
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
    source_config_file_sha256: Optional[str] = None


def build_run_metadata(
    scenario_name: str,
    config_path: str | Path,
    diagnostics: "ScenarioDiagnostics",
    competitor_rate_source: Optional[str] = None,
    timestamp: datetime | None = None,
    config: Optional["EngineConfig"] = None,
) -> RunMetadata:
    """Build a RunMetadata record from a completed ScenarioDiagnostics.

    ``config_hash`` is the canonical hash of the effective (validated) config that was
    priced -- the same definition as ``decision_record.config_hash``.
    ``source_config_file_sha256`` is the SHA-256 of the original file at ``config_path``.
    When ``config`` is omitted (legacy callers), the effective config is the one loaded
    from ``config_path``.
    """
    from balance_pricing_engine.config import load_config

    effective_config = config if config is not None else load_config(config_path)
    vi = sys.version_info
    run_timestamp = timestamp or datetime.now(timezone.utc)
    return RunMetadata(
        run_id=str(uuid.uuid4()),
        scenario_name=scenario_name,
        timestamp=run_timestamp.isoformat(),
        git_commit=_git_commit(),
        config_path=str(config_path),
        config_hash=effective_config_hash(effective_config),
        python_version=f"{vi.major}.{vi.minor}.{vi.micro}",
        package_version=_package_version(),
        boundary_warning_flag=diagnostics.boundary_diagnostics.has_warning,
        initial_boundary_warning_flag=diagnostics.initial_boundary_diagnostics.has_warning,
        expansion_rounds=diagnostics.expansion_rounds,
        policy_band_breached=diagnostics.policy_band_breached,
        competitor_rate_source=competitor_rate_source,
        source_config_file_sha256=file_sha256_if_present(config_path),
    )


def write_run_metadata(metadata: RunMetadata, run_dir: Path) -> None:
    """Serialise RunMetadata to metadata.json inside run_dir."""
    (run_dir / "metadata.json").write_text(
        json.dumps(asdict(metadata), indent=2, sort_keys=True),
        encoding="utf-8",
    )
