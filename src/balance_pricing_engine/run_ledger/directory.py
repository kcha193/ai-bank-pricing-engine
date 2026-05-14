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
    run_dir = Path(base_dir).resolve() / f"{timestamp}_{safe_name}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir
