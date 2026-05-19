from types import SimpleNamespace

import numpy as np
import pandas as pd

from balance_pricing_engine import plotting


class FakeAxisFormatters:
    def __init__(self) -> None:
        self.formatters = []

    def set_major_formatter(self, formatter) -> None:
        self.formatters.append(formatter)


class FakeAxis:
    def __init__(self) -> None:
        self.calls = []
        self.xaxis = FakeAxisFormatters()
        self.yaxis = FakeAxisFormatters()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return f"{name}-result"

        return method


class FakeFigure:
    def __init__(self) -> None:
        self.calls = []

    def colorbar(self, *args, **kwargs) -> None:
        self.calls.append(("colorbar", args, kwargs))

    def suptitle(self, *args, **kwargs) -> None:
        self.calls.append(("suptitle", args, kwargs))


class FakePyplot:
    def __init__(self) -> None:
        self.figures = []
        self.axes = []
        self.tight_layout_calls = 0
        self.show_calls = 0

    def subplots(self, rows=1, cols=1, figsize=None):
        figure = FakeFigure()
        self.figures.append(figure)
        if rows == 1 and cols == 1:
            axis = FakeAxis()
            self.axes.append(axis)
            return figure, axis

        if rows == 1:
            axes = np.array([FakeAxis() for _ in range(cols)], dtype=object)
            self.axes.extend(axes.tolist())
            return figure, axes
        if cols == 1:
            axes = np.array([FakeAxis() for _ in range(rows)], dtype=object)
            self.axes.extend(axes.tolist())
            return figure, axes

        axes = np.array([[FakeAxis() for _ in range(cols)] for _ in range(rows)], dtype=object)
        self.axes.extend(axes.flatten().tolist())
        return figure, axes

    def tight_layout(self) -> None:
        self.tight_layout_calls += 1

    def show(self) -> None:
        self.show_calls += 1

    def savefig(self, path, **kwargs) -> None:
        self.savefig_calls = getattr(self, "savefig_calls", [])
        self.savefig_calls.append(path)

    def close(self, fig=None) -> None:
        self.close_calls = getattr(self, "close_calls", [])
        self.close_calls.append(fig)


def fake_formatter(*args, **kwargs):
    return {"args": args, "kwargs": kwargs}


def install_fake_plotting(monkeypatch) -> FakePyplot:
    fake_plt = FakePyplot()
    monkeypatch.setattr(
        plotting,
        "get_plotting_tools",
        lambda: (fake_plt, fake_formatter, fake_formatter),
    )
    return fake_plt


def test_plot_customer_profiles_draws_synthetic_portfolio_histograms(monkeypatch):
    fake_plt = install_fake_plotting(monkeypatch)
    hist_calls = []

    def fake_hist(series, ax, bins, color, edgecolor, alpha):
        hist_calls.append(
            {
                "series": series.name,
                "ax": ax,
                "bins": bins,
                "color": color,
                "edgecolor": edgecolor,
                "alpha": alpha,
            }
        )
        return ax

    monkeypatch.setattr(pd.Series, "hist", fake_hist)
    loan_customers = pd.DataFrame(
        {
            "loan_amount": [100_000, 200_000],
            "credit_score": [700, 720],
            "lvr": [0.6, 0.8],
        }
    )
    deposit_customers = pd.DataFrame(
        {
            "deposit_amount": [50_000, 75_000],
            "rate_sensitivity": [0.2, 0.4],
            "relationship_years": [2.0, 5.0],
        }
    )

    plotting.plot_customer_profiles(loan_customers, deposit_customers)

    assert [call["series"] for call in hist_calls] == [
        "loan_amount",
        "credit_score",
        "lvr",
        "deposit_amount",
        "rate_sensitivity",
        "relationship_years",
    ]
    assert fake_plt.tight_layout_calls == 1
    assert fake_plt.show_calls == 1


def test_plot_objective_decomposition_draws_positive_and_negative_components(monkeypatch):
    fake_plt = install_fake_plotting(monkeypatch)
    objective_decomposition = pd.DataFrame(
        {
            "component": ["income", "cost", "total objective"],
            "amount": [100.0, -25.0, 75.0],
        }
    )

    plotting.plot_objective_decomposition(objective_decomposition)

    axis = fake_plt.axes[0]
    called_methods = [call[0] for call in axis.calls]
    assert "barh" in called_methods
    assert called_methods.count("text") == 2
    assert fake_plt.show_calls == 1


def test_plot_grid_search_results_draws_surface_and_candidate_views(monkeypatch):
    fake_plt = install_fake_plotting(monkeypatch)
    grid_results = pd.DataFrame(
        {
            "loan_rate": [0.05, 0.05, 0.06, 0.06],
            "deposit_rate": [0.03, 0.04, 0.03, 0.04],
            "total_objective": [10.0, 15.0, 20.0, 25.0],
            "funding_shortfall": [0.0, 5.0, 0.0, 10.0],
        }
    )
    best_by_loan_rate = pd.DataFrame(
        {"loan_rate": [0.05, 0.06], "total_objective": [15.0, 25.0]}
    )
    best_by_deposit_rate = pd.DataFrame(
        {"deposit_rate": [0.03, 0.04], "total_objective": [20.0, 25.0]}
    )
    top_candidates = grid_results.sort_values("total_objective", ascending=False)
    result = SimpleNamespace(loan_rate=0.06, deposit_rate=0.04)

    plotting.plot_grid_search_results(
        grid_results,
        best_by_loan_rate,
        best_by_deposit_rate,
        top_candidates,
        result,
    )

    called_methods = [call[0] for axis in fake_plt.axes for call in axis.calls]
    assert "imshow" in called_methods
    assert called_methods.count("scatter") >= 2
    assert called_methods.count("plot") == 2
    assert fake_plt.figures[0].calls[0][0] == "colorbar"
    assert fake_plt.show_calls == 1


def test_plot_sensitivity_summary_draws_objective_and_rate_movements(monkeypatch):
    fake_plt = install_fake_plotting(monkeypatch)
    sensitivity_summary = pd.DataFrame(
        {
            "scenario_name": ["baseline", "stress"],
            "objective_change_vs_baseline": [0.0, -10.0],
            "loan_rate_change_vs_baseline": [0.0, 0.001],
            "deposit_rate_change_vs_baseline": [0.0, -0.001],
        }
    )

    plotting.plot_sensitivity_summary(sensitivity_summary)

    called_methods = [call[0] for axis in fake_plt.axes for call in axis.calls]
    assert called_methods.count("barh") == 3
    assert "set_yticklabels" in called_methods
    assert fake_plt.show_calls == 1


def test_plot_response_model_report_draws_calibration_and_response_curves(monkeypatch):
    fake_plt = install_fake_plotting(monkeypatch)
    calibration = pd.DataFrame(
        {
            "mean_predicted_probability": [0.2, 0.8],
            "observed_rate": [0.25, 0.75],
            "count": [10, 20],
        }
    )
    loan_model_report = {"calibration": calibration}
    deposit_model_report = {"calibration": calibration}
    loan_response_check = SimpleNamespace(
        rate_response=pd.DataFrame(
            {"offered_rate": [0.05, 0.06], "mean_predicted_probability": [0.8, 0.7]}
        )
    )
    deposit_response_check = SimpleNamespace(
        rate_response=pd.DataFrame(
            {"offered_rate": [0.03, 0.04], "mean_predicted_probability": [0.3, 0.4]}
        )
    )

    plotting.plot_response_model_report(
        loan_model_report,
        deposit_model_report,
        loan_response_check,
        deposit_response_check,
        competitor_loan_rate=0.055,
        competitor_deposit_rate=0.035,
    )

    called_methods = [call[0] for axis in fake_plt.axes for call in axis.calls]
    assert called_methods.count("scatter") == 2
    assert called_methods.count("plot") == 4
    assert fake_plt.show_calls == 1


def test_beginner_visual_helpers_draw_expected_charts(monkeypatch):
    fake_plt = install_fake_plotting(monkeypatch)
    loan_customers = pd.DataFrame(
        {
            "loan_amount": [100_000, 200_000, 300_000],
            "credit_score": [700, 720, 740],
            "lvr": [0.6, 0.7, 0.8],
            "risk_score": [0.1, 0.2, 0.3],
        }
    )
    deposit_customers = pd.DataFrame(
        {
            "deposit_amount": [50_000, 75_000, 90_000],
            "rate_sensitivity": [0.2, 0.4, 0.6],
            "relationship_years": [2.0, 5.0, 8.0],
        }
    )
    grid_results = pd.DataFrame(
        {
            "loan_rate": [0.05, 0.05, 0.06, 0.06],
            "deposit_rate": [0.03, 0.04, 0.03, 0.04],
            "total_objective": [10.0, 15.0, 20.0, 25.0],
            "expected_loan_volume": [100.0, 100.0, 90.0, 90.0],
            "expected_deposit_volume": [70.0, 80.0, 70.0, 80.0],
            "funding_shortfall": [30.0, 20.0, 20.0, 10.0],
        }
    )
    loan_dataset = pd.DataFrame(
        {
            "offered_rate": [0.05, 0.05, 0.06, 0.06],
            "accepted": [1, 1, 0, 1],
            "ground_truth_probability": [0.8, 0.7, 0.5, 0.4],
        }
    )
    deposit_dataset = pd.DataFrame(
        {
            "offered_rate": [0.03, 0.03, 0.04, 0.04],
            "opened": [0, 1, 1, 1],
            "ground_truth_probability": [0.4, 0.5, 0.7, 0.8],
        }
    )
    candidate_summary = pd.DataFrame(
        {
            "product": ["Home loan", "Home loan"],
            "candidate": ["ElasticityBaselineModel", "LogisticResponseModel"],
            "log_loss": [0.4, 0.35],
            "governance_score": [0.9, 0.8],
        }
    )
    recommendations = pd.DataFrame({"recommend_as": ["baseline_champion"]})
    registry = pd.DataFrame({"champion_status": ["baseline_champion", "challenger_candidate"]})
    score_result = {
        "rows": 10,
        "mean_probability": 0.42,
        "used_fallback": True,
    }

    plotting.plot_workflow_map()
    plotting.plot_rate_grid_story(np.array([0.05, 0.06]), np.array([0.03, 0.04]), 0.055, 0.035)
    plotting.plot_customer_relationships(loan_customers, deposit_customers)
    plotting.plot_volume_and_funding_story(
        grid_results,
        SimpleNamespace(loan_rate=0.06, deposit_rate=0.04),
    )
    plotting.plot_response_dataset_overview(loan_dataset, deposit_dataset)
    plotting.plot_governed_modelling_summary(
        candidate_summary,
        recommendations,
        registry,
        score_result,
    )

    called_methods = [call[0] for axis in fake_plt.axes for call in axis.calls]
    assert "annotate" in called_methods
    assert called_methods.count("scatter") >= 4
    assert called_methods.count("bar") >= 6
    assert called_methods.count("plot") >= 6
    assert fake_plt.show_calls == 6


def test_plot_workflow_map_keeps_step_names_readable(monkeypatch):
    fake_plt = install_fake_plotting(monkeypatch)

    plotting.plot_workflow_map()

    axis = fake_plt.axes[0]
    text_calls = [call for call in axis.calls if call[0] == "text"]
    circle_labels = [call[1][2] for call in text_calls if call[2].get("color") == "white"]
    visible_titles = [
        call[1][2]
        for call in text_calls
        if call[2].get("fontweight") == "bold" and call[2].get("color") != "white"
    ]

    assert circle_labels == ["1", "2", "3", "4", "5", "6", "7"]
    assert visible_titles == [
        "Assumptions",
        "Synthetic customers",
        "Rate grid",
        "Optimizer",
        "Diagnostics",
        "Response models",
        "Governance",
    ]


def test_plotting_functions_return_without_matplotlib_tools(monkeypatch):
    monkeypatch.setattr(plotting, "get_plotting_tools", lambda: None)

    plotting.plot_customer_profiles(pd.DataFrame(), pd.DataFrame())
    plotting.plot_workflow_map()
    plotting.plot_rate_grid_story(np.array([]), np.array([]), 0.0, 0.0)
    plotting.plot_customer_relationships(pd.DataFrame(), pd.DataFrame())
    plotting.plot_objective_decomposition(pd.DataFrame())
    plotting.plot_volume_and_funding_story(pd.DataFrame(), SimpleNamespace())
    plotting.plot_grid_search_results(
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(),
        SimpleNamespace(loan_rate=0.0, deposit_rate=0.0),
    )
    plotting.plot_response_dataset_overview(pd.DataFrame(), pd.DataFrame())
    plotting.plot_sensitivity_summary(pd.DataFrame())
    plotting.plot_governed_modelling_summary(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), {})
    plotting.plot_response_model_report({}, {}, SimpleNamespace(), SimpleNamespace(), 0.0, 0.0)


# --- P2-7: plot_profit_surface ---


def test_plot_profit_surface_calls_savefig_and_close(monkeypatch, tmp_path):
    fake_plt = install_fake_plotting(monkeypatch)

    grid_rows = []
    for lr in [0.080, 0.085]:
        for dr in [0.050, 0.055]:
            grid_rows.append(
                {
                    "loan_rate": lr,
                    "deposit_rate": dr,
                    "total_objective": 2_000_000.0,
                    "funding_shortfall": 100_000.0,
                }
            )
    grid_results = pd.DataFrame(grid_rows)
    result = SimpleNamespace(loan_rate=0.080, deposit_rate=0.050)
    output_path = tmp_path / "surface.png"

    plotting.plot_profit_surface(grid_results, result, output_path)

    assert hasattr(fake_plt, "savefig_calls")
    assert len(fake_plt.savefig_calls) == 1
    assert str(fake_plt.savefig_calls[0]) == str(output_path)
    assert hasattr(fake_plt, "close_calls")
    assert len(fake_plt.close_calls) == 1


def test_plot_profit_surface_no_op_on_empty_grid(monkeypatch, tmp_path):
    fake_plt = install_fake_plotting(monkeypatch)
    plotting.plot_profit_surface(
        pd.DataFrame(),
        SimpleNamespace(loan_rate=0.08, deposit_rate=0.05),
        tmp_path / "out.png",
    )
    assert not hasattr(fake_plt, "savefig_calls")
