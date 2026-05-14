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
