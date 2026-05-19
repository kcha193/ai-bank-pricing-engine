"""Interactive profit-surface dashboard.

Launch with:
    streamlit run scripts/streamlit_dashboard.py

Requires: pip install -e ".[dashboard]"
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import streamlit as st

from balance_pricing_engine.plotting import plot_profit_surface


_RUNS_DIR = Path(__file__).resolve().parent.parent / "data" / "outputs" / "runs"


def _list_runs() -> list[str]:
    if not _RUNS_DIR.exists():
        return []
    return sorted([d.name for d in _RUNS_DIR.iterdir() if d.is_dir()], reverse=True)


def _load_run(run_name: str) -> tuple[pd.DataFrame, dict]:
    run_dir = _RUNS_DIR / run_name
    grid_path = run_dir / "grid_results.csv"
    summary_path = run_dir / "summary.json"
    if not grid_path.exists() or not summary_path.exists():
        st.error(
            f"Run '{run_name}' is missing grid_results.csv or summary.json. "
            "Run examples/run_baseline.py to generate a run."
        )
        st.stop()
    grid_results = pd.read_csv(grid_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    return grid_results, summary


def main() -> None:
    st.set_page_config(page_title="Profit Surface Dashboard", layout="wide")
    st.title("Pricing Optimisation — Profit Surface Dashboard")

    runs = _list_runs()
    if not runs:
        st.warning(
            f"No run directories found under `{_RUNS_DIR}`. "
            "Run `python3 examples/run_baseline.py` first."
        )
        return

    with st.sidebar:
        st.header("Run")
        selected_run = st.selectbox("Select run", runs)
        max_obj_millions = 20.0
        min_objective = st.slider(
            "Min objective ($M)", min_value=0.0, max_value=max_obj_millions, value=0.0, step=0.1
        )

    grid_results, summary = _load_run(selected_run)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Best loan rate", f"{summary['best_loan_rate']:.2%}")
    col2.metric("Best deposit rate", f"{summary['best_deposit_rate']:.2%}")
    col3.metric("Objective", f"${summary['objective_value'] / 1_000_000:.2f}M")
    col4.metric("Funding shortfall", f"${summary['funding_shortfall'] / 1_000_000:.2f}M")

    st.subheader("Profit surface")
    result_proxy = SimpleNamespace(
        loan_rate=summary["best_loan_rate"],
        deposit_rate=summary["best_deposit_rate"],
    )
    buf = io.BytesIO()
    plot_profit_surface(grid_results, result_proxy, buf)
    buf.seek(0)
    st.image(buf, use_container_width=True)

    threshold = min_objective * 1_000_000
    filtered = (
        grid_results[grid_results["total_objective"] >= threshold]
        .sort_values("total_objective", ascending=False)
        .head(20)
        .reset_index(drop=True)
    )
    st.subheader(f"Top candidates (objective ≥ ${min_objective:.1f}M)")
    st.dataframe(
        filtered[["loan_rate", "deposit_rate", "total_objective", "funding_shortfall"]]
    )


if __name__ == "__main__":
    main()
