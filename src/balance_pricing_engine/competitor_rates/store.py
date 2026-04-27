"""Stable artifact storage for competitor-rate runs."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from balance_pricing_engine.competitor_rates.models import CompetitorRateRun


def load_rate_run(path: str | Path) -> CompetitorRateRun:
    """Load a canonical competitor-rate run from JSON."""
    return CompetitorRateRun.model_validate_json(Path(path).read_text(encoding="utf-8"))


def write_rate_run(run: CompetitorRateRun, output_dir: str | Path) -> None:
    """Write latest JSON/CSV, immutable history, and validation report artifacts."""
    output_path = Path(output_dir)
    history_path = output_path / "history"
    output_path.mkdir(parents=True, exist_ok=True)
    history_path.mkdir(parents=True, exist_ok=True)

    json_text = run.model_dump_json(indent=2)
    (output_path / "latest.json").write_text(json_text, encoding="utf-8")
    history_name = run.captured_at_utc.strftime("%Y-%m-%dT%H-%M-%SZ.json")
    history_file = history_path / history_name
    if history_file.exists():
        history_file = history_path / f"{run.run_id}_{history_name}"
    history_file.write_text(json_text, encoding="utf-8")

    rows = [record.model_dump(mode="json") for record in run.records]
    pd.DataFrame(rows).to_csv(output_path / "latest.csv", index=False)
    (output_path / "validation_report_latest.json").write_text(
        json.dumps(run.validation_summary.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )
