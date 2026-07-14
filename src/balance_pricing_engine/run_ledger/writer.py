"""Orchestrator for the pricing run ledger."""

from __future__ import annotations

from datetime import datetime, timezone
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
    base_dir: str | Path = "data/outputs/runs",
    effective_config_path: str | Path | None = None,
) -> Path:
    """Create a full run ledger directory and write all artifacts.

    ``config_path`` is recorded in metadata.json verbatim as the caller's
    original config source. ``effective_config_path``, when given, is what
    gets hashed and snapshotted instead -- pass it when ``config_path`` no
    longer reflects what was actually priced (e.g. the caller enabled
    competitor-rate resolution, which replaces rates in ``config`` after
    ``config_path`` was set).

    Returns the path to the created run directory.
    """
    snapshot_source = effective_config_path if effective_config_path is not None else config_path
    run_timestamp = datetime.now(timezone.utc)
    run_dir = create_run_directory(scenario_name, base_dir, timestamp=run_timestamp)
    metadata = build_run_metadata(
        scenario_name=scenario_name,
        config_path=config_path,
        diagnostics=diagnostics,
        competitor_rate_source=competitor_rate_source,
        timestamp=run_timestamp,
        config_hash_source=snapshot_source,
    )
    write_run_metadata(metadata, run_dir)
    write_config_snapshot(snapshot_source, run_dir)
    diagnostics.write_artifacts(run_dir, sensitivity=sensitivity)
    write_pricing_memo(run_dir, metadata, config, diagnostics, sensitivity)
    write_governance_checklist(run_dir, metadata, config, diagnostics)
    return run_dir
