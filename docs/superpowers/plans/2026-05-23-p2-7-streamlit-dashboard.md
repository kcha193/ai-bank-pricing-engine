# P2-7: Streamlit Dashboard — Profit Surface + Sensitivity Waterfall

> **STATUS: ✅ COMPLETE** — Streamlit profit-surface + waterfall dashboard — PR #36

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Streamlit app that accepts a YAML config in a sidebar text area, runs the pricing engine, and shows three tabs: a summary metrics panel, a 2×2 profit-surface heat map, and a sensitivity waterfall bar chart.

**Architecture:** New `src/balance_pricing_engine/dashboard/` package with a single `app.py`. The app calls `run_baseline_diagnostics` inline (Streamlit caches the run via `st.cache_data`). The 2×2 profit-surface figure reuses `plot_profit_surface` from `plotting.py` — but that function saves to a file path. We add a `build_profit_surface_figure` helper that returns the `matplotlib.figure.Figure` instead of saving, so Streamlit can display it with `st.pyplot(fig)`. The sensitivity waterfall is a new `build_sensitivity_waterfall_figure` in `plotting.py`. A new `[dashboard]` extra in `pyproject.toml` already lists `streamlit>=1.30` and `matplotlib>=3.8`.

**Tech Stack:** Python 3.9+, Streamlit (optional `[dashboard]` extra already declared), matplotlib (same extra), pytest. The dashboard is not tested with Streamlit's test runner — unit tests cover only the two new matplotlib helpers.

---

## File map

| Action | Path | Responsibility |
| --- | --- | --- |
| Create | `src/balance_pricing_engine/dashboard/__init__.py` | Empty package marker |
| Create | `src/balance_pricing_engine/dashboard/app.py` | Streamlit app |
| Modify | `src/balance_pricing_engine/plotting.py` | Add `build_profit_surface_figure`, `build_sensitivity_waterfall_figure` |
| Modify | `tests/unit/test_plotting.py` | Tests for two new figure-building helpers |

---

## Task 1: `build_profit_surface_figure` in `plotting.py`

**Files:**

- Modify: `src/balance_pricing_engine/plotting.py`
- Test: `tests/unit/test_plotting.py`

`plot_profit_surface` already builds the complete figure — it just calls `plt.savefig(output_path)` at the end. The new helper replicates the same logic but returns the `Figure` object.

- [ ] **Step 1: Read the existing plotting tests to understand the fixture pattern**

```bash
grep -n "def test_\|grid_results\|OptimizationResult" tests/unit/test_plotting.py | head -30
```

- [ ] **Step 2: Write the failing test**

Add to `tests/unit/test_plotting.py`:

```python
from balance_pricing_engine.plotting import build_profit_surface_figure, build_sensitivity_waterfall_figure


def _make_grid_results() -> pd.DataFrame:
    """Minimal 2×2 grid results DataFrame for plotting tests."""
    import pandas as pd
    rows = []
    for loan_rate in [0.08, 0.085]:
        for deposit_rate in [0.05, 0.055]:
            rows.append({
                "loan_rate": loan_rate,
                "deposit_rate": deposit_rate,
                "total_objective": 12_000_000 + loan_rate * 1e8 - deposit_rate * 5e7,
                "loan_interest_income": 10_000_000,
                "expected_credit_loss": 500_000,
                "capital_charge": 200_000,
                "deposit_interest_cost": 2_000_000,
                "wholesale_funding_cost": 500_000,
                "excess_deposit_income": 0.0,
                "funding_shortfall_penalty": 0.0,
                "expected_loan_volume": 100_000_000,
                "expected_deposit_volume": 90_000_000,
                "funding_shortfall": 0.0,
                "home_loan_spread_to_market_bps": float("nan"),
                "term_deposit_spread_to_market_bps": float("nan"),
            })
    return pd.DataFrame(rows)


def _make_best_result():
    from balance_pricing_engine.optimization.grid_search import OptimizationResult
    return OptimizationResult(
        loan_rate=0.085,
        deposit_rate=0.05,
        loan_interest_income=10_500_000,
        expected_credit_loss=500_000,
        capital_charge=200_000,
        deposit_interest_cost=1_800_000,
        wholesale_funding_cost=500_000,
        excess_deposit_income=0.0,
        funding_shortfall_penalty=0.0,
        total_objective=12_500_000,
        expected_loan_volume=105_000_000,
        expected_deposit_volume=90_000_000,
        funding_shortfall=0.0,
    )


def test_build_profit_surface_figure_returns_figure():
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure

    fig = build_profit_surface_figure(_make_grid_results(), _make_best_result())

    assert isinstance(fig, Figure)


def test_build_profit_surface_figure_returns_none_for_empty_grid():
    import matplotlib
    matplotlib.use("Agg")
    import pandas as pd

    result = build_profit_surface_figure(pd.DataFrame(), _make_best_result())

    assert result is None


def test_build_sensitivity_waterfall_figure_returns_figure():
    import matplotlib
    matplotlib.use("Agg")
    import pandas as pd
    from matplotlib.figure import Figure

    sensitivity = pd.DataFrame({
        "scenario_name": ["baseline", "loan_rate_elasticity_up", "deposit_rate_elasticity_down"],
        "delta_objective_pct": [0.0, -3.2, 2.1],
        "parameter_family": ["baseline", "elasticity", "elasticity"],
    })

    fig = build_sensitivity_waterfall_figure(sensitivity)

    assert isinstance(fig, Figure)


def test_build_sensitivity_waterfall_figure_returns_none_for_empty_frame():
    import matplotlib
    matplotlib.use("Agg")
    import pandas as pd

    result = build_sensitivity_waterfall_figure(pd.DataFrame(
        columns=["scenario_name", "delta_objective_pct", "parameter_family"]
    ))

    assert result is None
```

- [ ] **Step 3: Run tests to confirm they fail**

```bash
python3 -m pytest tests/unit/test_plotting.py::test_build_profit_surface_figure_returns_figure -v
```

Expected: `ImportError` — `build_profit_surface_figure` not defined.

- [ ] **Step 4: Implement the two helpers at the end of `plotting.py`**

Add after the existing `plot_profit_surface` function:

```python
def build_profit_surface_figure(
    grid_results: pd.DataFrame,
    result: Any,
) -> Any:
    """Return a 2×2 matplotlib Figure of the profit surface, or None if grid is empty."""
    if len(grid_results) == 0:
        return None
    plotting = get_plotting_tools()
    if plotting is None:
        return None
    plt, func_formatter, percent_formatter = plotting

    pivot = (
        grid_results.pivot_table(
            index="deposit_rate",
            columns="loan_rate",
            values="total_objective",
            aggfunc="max",
        )
        .sort_index()
        .sort_index(axis=1)
    )
    loan_rates = pivot.columns.to_numpy(dtype=float)
    deposit_rates = pivot.index.to_numpy(dtype=float)
    best_loan_idx = int(np.abs(loan_rates - result.loan_rate).argmin())
    best_deposit_idx = int(np.abs(deposit_rates - result.deposit_rate).argmin())

    best_by_loan = (
        grid_results.groupby("loan_rate", as_index=False)["total_objective"]
        .max()
        .sort_values("loan_rate")
    )
    best_by_deposit = (
        grid_results.groupby("deposit_rate", as_index=False)["total_objective"]
        .max()
        .sort_values("deposit_rate")
    )
    top_10 = grid_results.sort_values("total_objective", ascending=False).head(10).copy()

    fig, axes = plt.subplots(2, 2, figsize=(15, 10))

    heatmap = axes[0, 0].imshow(
        pivot.to_numpy() / 1_000_000,
        aspect="auto",
        origin="lower",
        cmap="viridis",
    )
    axes[0, 0].scatter(
        best_loan_idx,
        best_deposit_idx,
        marker="*",
        s=220,
        color=CHART_COLORS["highlight"],
        edgecolor="black",
    )
    axes[0, 0].set_title("Objective surface by rate pair")
    axes[0, 0].set_xlabel("Home loan rate")
    axes[0, 0].set_ylabel("Term deposit rate")
    axes[0, 0].set_xticks(range(len(loan_rates)))
    axes[0, 0].set_xticklabels([fmt_rate(r) for r in loan_rates], rotation=45, ha="right")
    axes[0, 0].set_yticks(range(len(deposit_rates)))
    axes[0, 0].set_yticklabels([fmt_rate(r) for r in deposit_rates])
    fig.colorbar(heatmap, ax=axes[0, 0], label="Objective ($M)")

    axes[0, 1].plot(
        best_by_loan["loan_rate"],
        best_by_loan["total_objective"],
        marker="o",
        color=CHART_COLORS["loan"],
    )
    axes[0, 1].axvline(
        result.loan_rate, color=CHART_COLORS["highlight"], linestyle="--", label="winner"
    )
    axes[0, 1].set_title("Best objective by home loan rate")
    axes[0, 1].set_xlabel("Home loan rate")
    axes[0, 1].set_ylabel("Objective")
    axes[0, 1].xaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[0, 1].yaxis.set_major_formatter(money_axis_formatter(func_formatter))
    axes[0, 1].legend()

    axes[1, 0].plot(
        best_by_deposit["deposit_rate"],
        best_by_deposit["total_objective"],
        marker="o",
        color=CHART_COLORS["deposit"],
    )
    axes[1, 0].axvline(
        result.deposit_rate, color=CHART_COLORS["highlight"], linestyle="--", label="winner"
    )
    axes[1, 0].set_title("Best objective by term deposit rate")
    axes[1, 0].set_xlabel("Term deposit rate")
    axes[1, 0].set_ylabel("Objective")
    axes[1, 0].xaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[1, 0].yaxis.set_major_formatter(money_axis_formatter(func_formatter))
    axes[1, 0].legend()

    axes[1, 1].scatter(
        top_10["loan_rate"],
        top_10["deposit_rate"],
        c=top_10["total_objective"] / 1_000_000,
        cmap="viridis",
        s=120,
        edgecolors="black",
        linewidths=0.5,
    )
    axes[1, 1].scatter(
        [result.loan_rate],
        [result.deposit_rate],
        marker="*",
        s=280,
        color=CHART_COLORS["highlight"],
        edgecolor="black",
        label="winner",
        zorder=5,
    )
    axes[1, 1].set_title("Top-10 rate combinations")
    axes[1, 1].set_xlabel("Home loan rate")
    axes[1, 1].set_ylabel("Term deposit rate")
    axes[1, 1].xaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[1, 1].yaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[1, 1].legend()

    fig.tight_layout()
    plt.close(fig)
    return fig


def build_sensitivity_waterfall_figure(sensitivity: pd.DataFrame) -> Any:
    """Return a horizontal bar chart Figure of sensitivity deltas, or None if frame is empty."""
    if sensitivity.empty:
        return None
    plotting = get_plotting_tools()
    if plotting is None:
        return None
    plt, _func_formatter, _percent_formatter = plotting

    non_baseline = sensitivity[sensitivity["scenario_name"] != "baseline"].copy()
    if non_baseline.empty:
        return None

    non_baseline = non_baseline.sort_values("delta_objective_pct")
    colors = [
        CHART_COLORS["positive"] if v >= 0 else CHART_COLORS["negative"]
        for v in non_baseline["delta_objective_pct"]
    ]

    fig, ax = plt.subplots(figsize=(10, max(4, len(non_baseline) * 0.4 + 1)))
    ax.barh(
        non_baseline["scenario_name"],
        non_baseline["delta_objective_pct"],
        color=colors,
        edgecolor="white",
        linewidth=0.5,
    )
    ax.axvline(0, color=CHART_COLORS["neutral"], linewidth=1.0)
    ax.set_xlabel("Δ Objective (%)")
    ax.set_title("Sensitivity — impact on objective by scenario")
    fig.tight_layout()
    plt.close(fig)
    return fig
```

- [ ] **Step 5: Run the plotting tests**

```bash
python3 -m pytest tests/unit/test_plotting.py -v -k "surface or waterfall"
```

Expected: all 4 new tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/balance_pricing_engine/plotting.py tests/unit/test_plotting.py
git commit -m "feat: add build_profit_surface_figure and build_sensitivity_waterfall_figure"
```

---

## Task 2: Streamlit dashboard `app.py`

**Files:**

- Create: `src/balance_pricing_engine/dashboard/__init__.py`
- Create: `src/balance_pricing_engine/dashboard/app.py`

No unit tests for the Streamlit app itself — testing it requires Streamlit's own test runner which is not in the current test suite. The matplotlib helper tests in Task 1 already cover the rendering logic. Verify the app launches correctly by running it manually.

- [ ] **Step 1: Create the package marker**

Create `src/balance_pricing_engine/dashboard/__init__.py` with empty content.

- [ ] **Step 2: Create `app.py`**

Create `src/balance_pricing_engine/dashboard/app.py`:

```python
"""Streamlit dashboard for the AI Bank Pricing Engine."""

from __future__ import annotations

import textwrap
from pathlib import Path

import streamlit as st

from balance_pricing_engine.config import EngineConfig
from balance_pricing_engine.diagnostics import run_sensitivity_analysis
from balance_pricing_engine.plotting import (
    build_profit_surface_figure,
    build_sensitivity_waterfall_figure,
)
from balance_pricing_engine.reporting import fmt_money, fmt_rate
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics

import yaml
from pydantic import ValidationError


_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent.parent.parent / "configs" / "base.yaml"


def _default_yaml() -> str:
    if _DEFAULT_CONFIG_PATH.exists():
        return _DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    return "# paste your config YAML here\n"


st.set_page_config(page_title="AI Bank Pricing Engine", layout="wide")
st.title("AI Bank Pricing Engine")

with st.sidebar:
    st.header("Config")
    config_yaml = st.text_area(
        "YAML config",
        value=_default_yaml(),
        height=400,
        help="Paste the full EngineConfig YAML. Matches configs/base.yaml format.",
    )
    include_sensitivity = st.checkbox("Include sensitivity analysis", value=False)
    run_button = st.button("Run scenario", type="primary")

if not run_button:
    st.info("Paste a YAML config in the sidebar and click **Run scenario**.")
    st.stop()

try:
    raw = yaml.safe_load(config_yaml)
    config = EngineConfig(**raw)
except (TypeError, ValidationError, yaml.YAMLError) as exc:
    st.error(f"Config error: {exc}")
    st.stop()


@st.cache_data(show_spinner="Running pricing engine…")
def _run(config_yaml_str: str) -> dict:
    cfg = EngineConfig(**yaml.safe_load(config_yaml_str))
    diag = run_baseline_diagnostics(cfg)
    sensitivity_frame = run_sensitivity_analysis(cfg) if include_sensitivity else None
    return {"diagnostics": diag, "sensitivity": sensitivity_frame}


result = _run(config_yaml)
diagnostics = result["diagnostics"]
sensitivity = result["sensitivity"]
best = diagnostics.best_result

tab_summary, tab_surface, tab_sensitivity = st.tabs(
    ["Summary", "Profit surface", "Sensitivity"]
)

with tab_summary:
    col1, col2, col3 = st.columns(3)
    col1.metric("Best loan rate", fmt_rate(best.loan_rate))
    col2.metric("Best deposit rate", fmt_rate(best.deposit_rate))
    col3.metric("Objective", fmt_money(best.total_objective))

    col4, col5, col6 = st.columns(3)
    col4.metric("Loan volume", fmt_money(best.expected_loan_volume))
    col5.metric("Deposit volume", fmt_money(best.expected_deposit_volume))
    col6.metric("Funding shortfall", fmt_money(best.funding_shortfall))

    if diagnostics.boundary_diagnostics.has_warning:
        st.warning("Boundary warning — optimizer hit grid edge. Consider expanding the rate grid.")
    if diagnostics.policy_band_breached:
        st.error("Policy band breached — winning rate is outside the original policy grid.")

    st.subheader("Top results")
    st.dataframe(diagnostics.top_results(10), use_container_width=True)

with tab_surface:
    fig = build_profit_surface_figure(diagnostics.grid_results, best)
    if fig is not None:
        st.pyplot(fig)
    else:
        st.info("No grid results available.")

with tab_sensitivity:
    if sensitivity is None:
        st.info("Enable **Include sensitivity analysis** in the sidebar and re-run.")
    else:
        fig = build_sensitivity_waterfall_figure(sensitivity)
        if fig is not None:
            st.pyplot(fig)
        st.subheader("Sensitivity data")
        st.dataframe(sensitivity, use_container_width=True)
```

- [ ] **Step 3: Verify the dashboard launches without import errors**

```bash
python3 -c "from balance_pricing_engine.dashboard import app; print('import ok')"
```

Expected: `import ok` (Streamlit itself won't execute at import time).

- [ ] **Step 4: Launch the dashboard manually and verify the golden path**

```bash
python3 -m pip install -e ".[dashboard]" --quiet
streamlit run src/balance_pricing_engine/dashboard/app.py
```

Open the browser at `http://localhost:8501`. Verify:

1. Default config YAML loads into the sidebar.
2. Click **Run scenario** — spinner appears, then summary tab shows three rate/volume metrics.
3. Switch to **Profit surface** tab — 2×2 figure renders.
4. Enable **Include sensitivity analysis**, re-run — Sensitivity tab shows waterfall + data table.
5. Paste a broken YAML (`random_seed: [`), click Run — red error block appears, no crash.

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
python3 -m pytest -q
```

Expected: same pass count as before (dashboard has no new unit tests, but plotting tests added in Task 1 should pass).

- [ ] **Step 6: Commit**

```bash
git add src/balance_pricing_engine/dashboard/ 
git commit -m "feat: Streamlit profit-surface and sensitivity dashboard"
```

---

## Self-review

**Spec coverage:**

- Profit-surface heat map ✓ (`build_profit_surface_figure` → `st.pyplot`)
- Sensitivity waterfall bar chart ✓ (`build_sensitivity_waterfall_figure` → `st.pyplot`)
- Summary metrics panel ✓ (six `st.metric` cells + top-results table)
- Error handling for bad YAML ✓ (caught, shown as `st.error`, `st.stop()`)
- `[dashboard]` optional extra already declared ✓

**Placeholder scan:** Step 4 specifies exact manual verification steps with expected behaviour for each — not vague.

**Type consistency:** `build_profit_surface_figure` returns `Any` (matplotlib `Figure`) matching `st.pyplot(fig)` call. `build_sensitivity_waterfall_figure` same return type. Both return `None` on empty input, handled with `if fig is not None` in the app.
