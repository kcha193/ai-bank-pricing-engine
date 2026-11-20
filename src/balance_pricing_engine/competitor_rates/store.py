"""Stable artifact storage for competitor-rate runs."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

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

    rows = [
        {column: neutralise_csv_cell(value) for column, value in record.model_dump(mode="json").items()}
        for record in run.records
    ]
    pd.DataFrame(rows).to_csv(output_path / "latest.csv", index=False)
    (output_path / "validation_report_latest.json").write_text(
        json.dumps(run.validation_summary.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )


#: First characters a spreadsheet may read as the start of a formula (SEC-010): ``=``, ``+``,
#: ``-``, ``@`` and their full-width forms, which some spreadsheet locales also evaluate.
_FORMULA_PREFIXES = frozenset("=+-@\uff1d\uff0b\uff0d\uff20")
#: A plain decimal number is data, not a formula, even with a sign ("-0.84").
_PLAIN_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def neutralise_csv_cell(value: Any) -> Any:
    """Make one ``latest.csv`` cell inert when opened in spreadsheet software (SEC-010).

    Bank page text is untrusted. A string cell is prefixed with a single quote, which
    spreadsheets treat as "show as text", when it starts with a control character (tab,
    carriage return, newline or any other C0 control) or when its first non-whitespace
    character is a formula marker (see ``_FORMULA_PREFIXES``). Everything else is returned
    unchanged: non-strings (rates stay numeric, negatives included), plain signed numbers
    stored as text, and a value that already starts with a quote, so applying this twice
    never double-escapes.

    Presentation only: ``latest.json`` and the history JSON keep the original source text,
    and the record objects are never modified.
    """
    if not isinstance(value, str) or not value or _PLAIN_NUMBER.fullmatch(value):
        return value
    stripped = value.lstrip()
    if ord(value[0]) < 0x20 or (stripped and stripped[0] in _FORMULA_PREFIXES):
        return "'" + value
    return value
