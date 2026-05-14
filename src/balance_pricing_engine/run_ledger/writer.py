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
