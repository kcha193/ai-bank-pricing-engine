"""Run directory creation for the pricing run ledger."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def create_run_directory(
    scenario_name: str,
    base_dir: str | Path = "data/outputs/runs",
    timestamp: datetime | None = None,
) -> Path:
    """Create and return a unique timestamped run directory.

    Directory name: {YYYYMMDD_HHMMSS_microseconds}_{safe_scenario_name}
    """
    run_timestamp = timestamp or datetime.utcnow()
    timestamp_label = run_timestamp.strftime("%Y%m%d_%H%M%S_%f")
    safe_name = scenario_name.replace(" ", "_").replace("/", "_")
    base_path = Path(base_dir).resolve()
    name = f"{timestamp_label}_{safe_name}"

    attempt = 0
    while True:
        suffix = "" if attempt == 0 else f"_{attempt:03d}"
        run_dir = base_path / f"{name}{suffix}"
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            attempt += 1
            continue
        return run_dir
