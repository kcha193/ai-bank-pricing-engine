"""Optional matplotlib plotting helpers for pricing walkthroughs."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from balance_pricing_engine.reporting import fmt_money, fmt_rate


CHART_COLORS = {
    "loan": "#2f6f9f",
    "deposit": "#2f8f5b",
    "positive": "#2f8f5b",
    "negative": "#b85757",
    "highlight": "#d9902f",
    "neutral": "#6b7280",
    "purple": "#7c5cc4",
    "sky": "#5aa6c8",
}


def get_plotting_tools() -> tuple[Any, Any, Any] | None:
    """Import and configure matplotlib when it is available."""
    try:
        import matplotlib.pyplot as plt
        from matplotlib.ticker import FuncFormatter, PercentFormatter
    except ImportError:
        print("matplotlib is not installed. To enable charts, run: python3 -m pip install matplotlib")
        return None

    try:
        plt.style.use("seaborn-v0_8-whitegrid")
    except OSError:
        pass
    return plt, FuncFormatter, PercentFormatter


def money_axis_formatter(func_formatter: Any) -> Any:
    """Build a matplotlib formatter for million-dollar chart axes."""
    return func_formatter(lambda value, position: f"${value / 1_000_000:.1f}M")


def rate_axis_formatter(percent_formatter: Any) -> Any:
    """Build a matplotlib formatter for decimal-rate chart axes."""
    return percent_formatter(xmax=1.0, decimals=1)


def plot_customer_profiles(loan_customers: pd.DataFrame, deposit_customers: pd.DataFrame) -> None:
    """Plot histogram summaries of synthetic loan and deposit customers."""
    plotting = get_plotting_tools()
    if plotting is None:
        return
    plt, func_formatter, percent_formatter = plotting

    fig, axes = plt.subplots(2, 3, figsize=(15, 7.5))
    axes = axes.flatten()
    plot_specs = [
        (
            loan_customers["loan_amount"],
            "Requested home loan balances",
            "Loan amount",
            CHART_COLORS["loan"],
            money_axis_formatter(func_formatter),
        ),
        (loan_customers["credit_score"], "Credit scores", "Credit score", CHART_COLORS["loan"], None),
        (
            loan_customers["lvr"],
            "Loan-to-value ratios",
            "LVR",
            CHART_COLORS["loan"],
            rate_axis_formatter(percent_formatter),
        ),
        (
            deposit_customers["deposit_amount"],
            "Term deposit balances",
            "Deposit amount",
            CHART_COLORS["deposit"],
            money_axis_formatter(func_formatter),
        ),
        (
            deposit_customers["rate_sensitivity"],
            "Deposit rate sensitivity",
            "Sensitivity score",
            CHART_COLORS["deposit"],
            None,
        ),
        (
            deposit_customers["relationship_years"],
            "Relationship length",
            "Years",
            CHART_COLORS["deposit"],
            None,
        ),
    ]

    for ax, (series, title, xlabel, color, formatter) in zip(axes, plot_specs):
        series.hist(ax=ax, bins=30, color=color, edgecolor="white", alpha=0.9)
        ax.set_title(title)
        ax.set_xlabel(xlabel)
        ax.set_ylabel("Customers")
        if formatter is not None:
            ax.xaxis.set_major_formatter(formatter)

    fig.suptitle("Synthetic portfolio profile", fontsize=14, fontweight="bold")
    plt.tight_layout()
    plt.show()


def plot_workflow_map() -> None:
    """Plot a beginner-oriented map of the notebook workflow."""
    plotting = get_plotting_tools()
    if plotting is None:
        return
    plt, _, _ = plotting

    steps = [
        ("1. Assumptions", "Read the YAML config"),
        ("2. Synthetic customers", "Create safe fake portfolios"),
        ("3. Rate grid", "List prices to test"),
        ("4. Optimizer", "Score every rate pair"),
        ("5. Diagnostics", "Explain the winner"),
        ("6. Response models", "Check learned behaviour"),
        ("7. Governance", "Record, review, and score"),
    ]
    colors = [
        CHART_COLORS["neutral"],
        CHART_COLORS["loan"],
        CHART_COLORS["deposit"],
        CHART_COLORS["highlight"],
        CHART_COLORS["sky"],
        CHART_COLORS["purple"],
        CHART_COLORS["positive"],
    ]

    fig, ax = plt.subplots(figsize=(15, 3.8))
    for index, ((title, subtitle), color) in enumerate(zip(steps, colors)):
        ax.scatter(index, 0, s=900, color=color, edgecolor="black", linewidth=0.8, zorder=3)
        ax.text(index, 0.04, title, ha="center", va="center", color="white", fontweight="bold")
        ax.text(index, -0.34, subtitle, ha="center", va="top", fontsize=9)
        if index < len(steps) - 1:
            ax.annotate(
                "",
                xy=(index + 0.72, 0),
                xytext=(index + 0.28, 0),
                arrowprops={"arrowstyle": "->", "color": CHART_COLORS["neutral"], "lw": 1.6},
            )

    ax.set_title("Notebook workflow map")
    ax.set_xlim(-0.6, len(steps) - 0.4)
    ax.set_ylim(-0.75, 0.45)
    ax.axis("off")
    plt.tight_layout()
    plt.show()


def plot_rate_grid_story(
    loan_grid: np.ndarray,
    deposit_grid: np.ndarray,
    competitor_loan_rate: float,
    competitor_deposit_rate: float,
) -> None:
    """Plot candidate rates as simple rulers with competitor reference markers."""
    plotting = get_plotting_tools()
    if plotting is None:
        return
    plt, _, percent_formatter = plotting

    fig, axes = plt.subplots(2, 1, figsize=(13, 5.8))
    specs = [
        (
            axes[0],
            loan_grid,
            competitor_loan_rate,
            "Home loan candidate-rate ruler",
            "Candidate home loan rates",
            CHART_COLORS["loan"],
        ),
        (
            axes[1],
            deposit_grid,
            competitor_deposit_rate,
            "Term deposit candidate-rate ruler",
            "Candidate term deposit rates",
            CHART_COLORS["deposit"],
        ),
    ]

    for ax, rates, competitor_rate, title, xlabel, color in specs:
        y = np.zeros(len(rates))
        ax.scatter(rates, y, s=85, color=color, edgecolor="white", zorder=3)
        ax.axvline(
            competitor_rate,
            color=CHART_COLORS["highlight"],
            linestyle="--",
            linewidth=2,
            label="competitor reference",
        )
        ax.set_title(title)
        ax.set_xlabel(xlabel)
        ax.set_yticks([])
        ax.xaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
        ax.legend(loc="upper left")

    plt.tight_layout()
    plt.show()


def plot_customer_relationships(loan_customers: pd.DataFrame, deposit_customers: pd.DataFrame) -> None:
    """Plot simple relationships in the synthetic customer portfolios."""
    plotting = get_plotting_tools()
    if plotting is None:
        return
    plt, func_formatter, percent_formatter = plotting

    fig, axes = plt.subplots(1, 2, figsize=(14, 5.2))
    loan_sample = loan_customers.head(1_500)
    deposit_sample = deposit_customers.head(1_500)

    loan_scatter = axes[0].scatter(
        loan_sample["credit_score"],
        loan_sample["lvr"],
        c=loan_sample["risk_score"],
        cmap="YlOrRd",
        s=18,
        alpha=0.7,
        edgecolor="none",
    )
    axes[0].set_title("Home loan risk picture")
    axes[0].set_xlabel("Credit score")
    axes[0].set_ylabel("Loan-to-value ratio")
    axes[0].yaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    fig.colorbar(loan_scatter, ax=axes[0], label="Risk score")

    deposit_scatter = axes[1].scatter(
        deposit_sample["deposit_amount"],
        deposit_sample["rate_sensitivity"],
        c=deposit_sample["relationship_years"],
        cmap="viridis",
        s=18,
        alpha=0.7,
        edgecolor="none",
    )
    axes[1].set_title("Term deposit behaviour picture")
    axes[1].set_xlabel("Deposit amount")
    axes[1].set_ylabel("Rate sensitivity")
    axes[1].xaxis.set_major_formatter(money_axis_formatter(func_formatter))
    fig.colorbar(deposit_scatter, ax=axes[1], label="Relationship years")

    plt.tight_layout()
    plt.show()


def plot_objective_decomposition(objective_decomposition: pd.DataFrame) -> None:
    """Plot the winning objective decomposition as a horizontal bar chart."""
    plotting = get_plotting_tools()
    if plotting is None:
        return
    plt, func_formatter, _ = plotting

    chart_data = objective_decomposition[
        objective_decomposition["component"] != "total objective"
    ].copy()
    colors = [
        CHART_COLORS["positive"] if amount >= 0 else CHART_COLORS["negative"]
        for amount in chart_data["amount"]
    ]

    fig, ax = plt.subplots(figsize=(10, 4.8))
    ax.barh(chart_data["component"], chart_data["amount"], color=colors)
    ax.axvline(0, color="#333333", linewidth=1)
    ax.xaxis.set_major_formatter(money_axis_formatter(func_formatter))
    ax.set_title("Winning objective decomposition")
    ax.set_xlabel("Contribution to objective")

    label_offset = chart_data["amount"].abs().max() * 0.02
    for row_number, amount in enumerate(chart_data["amount"]):
        label_x = amount + label_offset if amount >= 0 else amount - label_offset
        alignment = "left" if amount >= 0 else "right"
        ax.text(label_x, row_number, fmt_money(amount), va="center", ha=alignment)

    plt.tight_layout()
    plt.show()


def plot_volume_and_funding_story(grid_results: pd.DataFrame, result: Any) -> None:
    """Plot how rates connect to volumes, objective, and funding shortfall."""
    plotting = get_plotting_tools()
    if plotting is None:
        return
    plt, func_formatter, percent_formatter = plotting

    loan_volume = (
        grid_results.groupby("loan_rate", as_index=False)["expected_loan_volume"]
        .mean()
        .sort_values("loan_rate")
    )
    deposit_volume = (
        grid_results.groupby("deposit_rate", as_index=False)["expected_deposit_volume"]
        .mean()
        .sort_values("deposit_rate")
    )
    top = grid_results.sort_values("total_objective", ascending=False).head(12).copy()
    top["candidate_label"] = [f"#{rank}" for rank in range(1, len(top) + 1)]

    fig, axes = plt.subplots(2, 2, figsize=(15, 9))

    axes[0, 0].plot(
        loan_volume["loan_rate"],
        loan_volume["expected_loan_volume"],
        marker="o",
        color=CHART_COLORS["loan"],
    )
    axes[0, 0].axvline(result.loan_rate, color=CHART_COLORS["highlight"], linestyle="--", label="winner")
    axes[0, 0].set_title("Higher home loan rates usually reduce accepted lending")
    axes[0, 0].set_xlabel("Home loan rate")
    axes[0, 0].set_ylabel("Expected loan volume")
    axes[0, 0].xaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[0, 0].yaxis.set_major_formatter(money_axis_formatter(func_formatter))
    axes[0, 0].legend()

    axes[0, 1].plot(
        deposit_volume["deposit_rate"],
        deposit_volume["expected_deposit_volume"],
        marker="o",
        color=CHART_COLORS["deposit"],
    )
    axes[0, 1].axvline(
        result.deposit_rate,
        color=CHART_COLORS["highlight"],
        linestyle="--",
        label="winner",
    )
    axes[0, 1].set_title("Higher deposit rates usually attract more funding")
    axes[0, 1].set_xlabel("Term deposit rate")
    axes[0, 1].set_ylabel("Expected deposit volume")
    axes[0, 1].xaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[0, 1].yaxis.set_major_formatter(money_axis_formatter(func_formatter))
    axes[0, 1].legend()

    axes[1, 0].bar(
        top["candidate_label"],
        top["total_objective"],
        color=CHART_COLORS["highlight"],
        alpha=0.9,
    )
    axes[1, 0].set_title("Top candidates by objective")
    axes[1, 0].set_xlabel("Ranked candidate")
    axes[1, 0].set_ylabel("Objective")
    axes[1, 0].yaxis.set_major_formatter(money_axis_formatter(func_formatter))

    axes[1, 1].bar(
        top["candidate_label"],
        top["funding_shortfall"],
        color=CHART_COLORS["negative"],
        alpha=0.85,
    )
    axes[1, 1].set_title("Funding shortfall for the same candidates")
    axes[1, 1].set_xlabel("Ranked candidate")
    axes[1, 1].set_ylabel("Funding shortfall")
    axes[1, 1].yaxis.set_major_formatter(money_axis_formatter(func_formatter))

    plt.tight_layout()
    plt.show()


def plot_grid_search_results(
    grid_results: pd.DataFrame,
    best_by_loan_rate: pd.DataFrame,
    best_by_deposit_rate: pd.DataFrame,
    top_candidates: pd.DataFrame,
    result: Any,
) -> None:
    """Plot the optimizer objective surface and selected candidate summaries."""
    plotting = get_plotting_tools()
    if plotting is None:
        return
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
    best_loan_index = int(np.abs(loan_rates - result.loan_rate).argmin())
    best_deposit_index = int(np.abs(deposit_rates - result.deposit_rate).argmin())

    fig, axes = plt.subplots(2, 2, figsize=(15, 10))

    heatmap = axes[0, 0].imshow(
        pivot.to_numpy() / 1_000_000,
        aspect="auto",
        origin="lower",
        cmap="viridis",
    )
    axes[0, 0].scatter(
        best_loan_index,
        best_deposit_index,
        marker="*",
        s=220,
        color=CHART_COLORS["highlight"],
        edgecolor="black",
    )
    axes[0, 0].set_title("Objective surface by rate pair")
    axes[0, 0].set_xlabel("Home loan rate")
    axes[0, 0].set_ylabel("Term deposit rate")
    axes[0, 0].set_xticks(range(len(loan_rates)))
    axes[0, 0].set_xticklabels([fmt_rate(rate) for rate in loan_rates], rotation=45, ha="right")
    axes[0, 0].set_yticks(range(len(deposit_rates)))
    axes[0, 0].set_yticklabels([fmt_rate(rate) for rate in deposit_rates])
    fig.colorbar(heatmap, ax=axes[0, 0], label="Objective ($M)")

    axes[0, 1].plot(
        best_by_loan_rate["loan_rate"],
        best_by_loan_rate["total_objective"],
        marker="o",
        color=CHART_COLORS["loan"],
    )
    axes[0, 1].axvline(
        result.loan_rate,
        color=CHART_COLORS["highlight"],
        linestyle="--",
        label="winner",
    )
    axes[0, 1].set_title("Best objective by home loan rate")
    axes[0, 1].set_xlabel("Home loan rate")
    axes[0, 1].set_ylabel("Objective")
    axes[0, 1].xaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[0, 1].yaxis.set_major_formatter(money_axis_formatter(func_formatter))
    axes[0, 1].legend()

    axes[1, 0].plot(
        best_by_deposit_rate["deposit_rate"],
        best_by_deposit_rate["total_objective"],
        marker="o",
        color=CHART_COLORS["deposit"],
    )
    axes[1, 0].axvline(
        result.deposit_rate,
        color=CHART_COLORS["highlight"],
        linestyle="--",
        label="winner",
    )
    axes[1, 0].set_title("Best objective by term deposit rate")
    axes[1, 0].set_xlabel("Term deposit rate")
    axes[1, 0].set_ylabel("Objective")
    axes[1, 0].xaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[1, 0].yaxis.set_major_formatter(money_axis_formatter(func_formatter))
    axes[1, 0].legend()

    top_10 = top_candidates.head(10).copy()
    shortfall = top_10["funding_shortfall"].clip(lower=0)
    sizes = 90 + 320 * shortfall / shortfall.max() if shortfall.max() > 0 else 120
    scatter = axes[1, 1].scatter(
        top_10["loan_rate"],
        top_10["deposit_rate"],
        c=top_10["total_objective"] / 1_000_000,
        s=sizes,
        cmap="magma",
        edgecolor="black",
        linewidth=0.6,
        alpha=0.85,
    )
    axes[1, 1].scatter(
        result.loan_rate,
        result.deposit_rate,
        marker="*",
        s=240,
        color=CHART_COLORS["highlight"],
        edgecolor="black",
        label="winner",
    )
    axes[1, 1].set_title("Top 10 pricing candidates")
    axes[1, 1].set_xlabel("Home loan rate")
    axes[1, 1].set_ylabel("Term deposit rate")
    axes[1, 1].xaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[1, 1].yaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[1, 1].legend()
    fig.colorbar(scatter, ax=axes[1, 1], label="Objective ($M)")

    plt.tight_layout()
    plt.show()


def plot_response_dataset_overview(
    loan_dataset: pd.DataFrame,
    deposit_dataset: pd.DataFrame,
) -> None:
    """Plot observed synthetic response rates before model fitting."""
    plotting = get_plotting_tools()
    if plotting is None:
        return
    plt, _, percent_formatter = plotting

    loan_observed = _observed_response_by_rate(loan_dataset, "accepted")
    deposit_observed = _observed_response_by_rate(deposit_dataset, "opened")

    fig, axes = plt.subplots(1, 2, figsize=(14, 5.2))
    axes[0].plot(
        loan_observed["offered_rate"],
        loan_observed["observed_response_rate"],
        marker="o",
        color=CHART_COLORS["loan"],
        label="observed acceptance",
    )
    axes[0].plot(
        loan_observed["offered_rate"],
        loan_observed["mean_ground_truth_probability"],
        linestyle="--",
        color=CHART_COLORS["neutral"],
        label="ground truth probability",
    )
    axes[0].set_title("Observed home loan acceptance by offered rate")
    axes[0].set_xlabel("Offered home loan rate")
    axes[0].set_ylabel("Acceptance rate")
    axes[0].xaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[0].yaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[0].legend()

    axes[1].plot(
        deposit_observed["offered_rate"],
        deposit_observed["observed_response_rate"],
        marker="o",
        color=CHART_COLORS["deposit"],
        label="observed uptake",
    )
    axes[1].plot(
        deposit_observed["offered_rate"],
        deposit_observed["mean_ground_truth_probability"],
        linestyle="--",
        color=CHART_COLORS["neutral"],
        label="ground truth probability",
    )
    axes[1].set_title("Observed term deposit uptake by offered rate")
    axes[1].set_xlabel("Offered term deposit rate")
    axes[1].set_ylabel("Uptake rate")
    axes[1].xaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[1].yaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[1].legend()

    plt.tight_layout()
    plt.show()


def plot_sensitivity_summary(sensitivity_summary: pd.DataFrame) -> None:
    """Plot sensitivity objective changes and rate movements."""
    plotting = get_plotting_tools()
    if plotting is None:
        return
    plt, func_formatter, _ = plotting

    chart_data = sensitivity_summary.copy()
    y_positions = np.arange(len(chart_data))

    fig, axes = plt.subplots(1, 2, figsize=(15, 5.5))

    objective_colors = [
        CHART_COLORS["positive"] if value >= 0 else CHART_COLORS["negative"]
        for value in chart_data["objective_change_vs_baseline"]
    ]
    axes[0].barh(
        chart_data["scenario_name"],
        chart_data["objective_change_vs_baseline"],
        color=objective_colors,
    )
    axes[0].axvline(0, color="#333333", linewidth=1)
    axes[0].xaxis.set_major_formatter(money_axis_formatter(func_formatter))
    axes[0].set_title("Objective change vs baseline")
    axes[0].set_xlabel("Change in objective")

    axes[1].barh(
        y_positions - 0.18,
        chart_data["loan_rate_change_vs_baseline"] * 10_000,
        height=0.35,
        label="home loan",
        color=CHART_COLORS["loan"],
    )
    axes[1].barh(
        y_positions + 0.18,
        chart_data["deposit_rate_change_vs_baseline"] * 10_000,
        height=0.35,
        label="term deposit",
        color=CHART_COLORS["deposit"],
    )
    axes[1].axvline(0, color="#333333", linewidth=1)
    axes[1].set_yticks(y_positions)
    axes[1].set_yticklabels(chart_data["scenario_name"])
    axes[1].set_title("Recommended rate movement")
    axes[1].set_xlabel("Change vs baseline (bps)")
    axes[1].legend()

    plt.tight_layout()
    plt.show()


def plot_governed_modelling_summary(
    candidate_summary: pd.DataFrame,
    recommendations: pd.DataFrame,
    registry: pd.DataFrame,
    score_result: dict[str, Any],
) -> None:
    """Plot beginner-friendly governed modelling diagnostics."""
    plotting = get_plotting_tools()
    if plotting is None:
        return
    plt, _, percent_formatter = plotting

    candidates = candidate_summary.copy()
    candidates["label"] = candidates["product"] + "\n" + candidates["candidate"]
    status_counts = registry["champion_status"].fillna("unknown").value_counts()
    recommendation_counts = recommendations["recommend_as"].fillna("unknown").value_counts()

    fig, axes = plt.subplots(2, 2, figsize=(15, 9))
    axes[0, 0].bar(
        candidates["label"],
        candidates["log_loss"],
        color=CHART_COLORS["purple"],
        alpha=0.9,
    )
    axes[0, 0].set_title("Candidate probability error")
    axes[0, 0].set_ylabel("Log loss (lower is better)")
    axes[0, 0].tick_params(axis="x", rotation=20)

    axes[0, 1].bar(
        candidates["label"],
        candidates["governance_score"],
        color=CHART_COLORS["positive"],
        alpha=0.9,
    )
    axes[0, 1].set_title("Governance score")
    axes[0, 1].set_ylabel("Score")
    axes[0, 1].tick_params(axis="x", rotation=20)

    axes[1, 0].bar(
        status_counts.index.astype(str),
        status_counts.to_numpy(),
        color=CHART_COLORS["sky"],
        alpha=0.9,
    )
    axes[1, 0].set_title("Registry champion/challenger labels")
    axes[1, 0].set_ylabel("Registry records")
    axes[1, 0].tick_params(axis="x", rotation=20)

    axes[1, 1].bar(
        recommendation_counts.index.astype(str),
        recommendation_counts.to_numpy(),
        color=CHART_COLORS["highlight"],
        alpha=0.9,
    )
    axes[1, 1].set_title("Recommendation types")
    axes[1, 1].set_ylabel("Products")
    axes[1, 1].tick_params(axis="x", rotation=20)
    axes[1, 1].text(
        0.02,
        0.92,
        (
            f"Scored rows: {score_result['rows']}\n"
            f"Mean probability: {fmt_rate(score_result['mean_probability'])}\n"
            f"Fallback used: {score_result['used_fallback']}"
        ),
        transform=axes[1, 1].transAxes,
        va="top",
        bbox={"boxstyle": "round,pad=0.35", "facecolor": "white", "edgecolor": CHART_COLORS["neutral"]},
    )

    plt.tight_layout()
    plt.show()


def plot_response_model_report(
    loan_model_report: dict[str, Any],
    deposit_model_report: dict[str, Any],
    loan_response_check: Any,
    deposit_response_check: Any,
    competitor_loan_rate: float,
    competitor_deposit_rate: float,
) -> None:
    """Plot calibration and response curves for the learned response models."""
    plotting = get_plotting_tools()
    if plotting is None:
        return
    plt, _, percent_formatter = plotting

    fig, axes = plt.subplots(2, 2, figsize=(13, 9))
    _plot_calibration(
        axes[0, 0],
        loan_model_report,
        "Home loan model calibration",
        percent_formatter,
    )
    _plot_calibration(
        axes[0, 1],
        deposit_model_report,
        "Term deposit model calibration",
        percent_formatter,
    )

    axes[1, 0].plot(
        loan_response_check.rate_response["offered_rate"],
        loan_response_check.rate_response["mean_predicted_probability"],
        marker="o",
        color=CHART_COLORS["loan"],
    )
    axes[1, 0].axvline(
        competitor_loan_rate,
        color=CHART_COLORS["neutral"],
        linestyle="--",
        label="competitor rate",
    )
    axes[1, 0].set_title("Home loan acceptance response")
    axes[1, 0].set_xlabel("Offered home loan rate")
    axes[1, 0].set_ylabel("Mean predicted acceptance")
    axes[1, 0].xaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[1, 0].yaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[1, 0].legend()

    axes[1, 1].plot(
        deposit_response_check.rate_response["offered_rate"],
        deposit_response_check.rate_response["mean_predicted_probability"],
        marker="o",
        color=CHART_COLORS["deposit"],
    )
    axes[1, 1].axvline(
        competitor_deposit_rate,
        color=CHART_COLORS["neutral"],
        linestyle="--",
        label="competitor rate",
    )
    axes[1, 1].set_title("Term deposit uptake response")
    axes[1, 1].set_xlabel("Offered term deposit rate")
    axes[1, 1].set_ylabel("Mean predicted uptake")
    axes[1, 1].xaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[1, 1].yaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    axes[1, 1].legend()

    plt.tight_layout()
    plt.show()


def _plot_calibration(ax: Any, report: dict[str, Any], title: str, percent_formatter: Any) -> None:
    calibration = report["calibration"]
    marker_sizes = 80 + 220 * calibration["count"] / calibration["count"].max()
    ax.plot([0, 1], [0, 1], color=CHART_COLORS["neutral"], linestyle="--", label="perfect calibration")
    ax.scatter(
        calibration["mean_predicted_probability"],
        calibration["observed_rate"],
        s=marker_sizes,
        color=CHART_COLORS["highlight"],
        edgecolor="black",
        alpha=0.85,
    )
    ax.set_title(title)
    ax.set_xlabel("Mean predicted probability")
    ax.set_ylabel("Observed response rate")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.xaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    ax.yaxis.set_major_formatter(rate_axis_formatter(percent_formatter))
    ax.legend()


def _observed_response_by_rate(dataset: pd.DataFrame, target_column: str) -> pd.DataFrame:
    grouped = (
        dataset.groupby("offered_rate", as_index=False)
        .agg(
            observed_response_rate=(target_column, "mean"),
            mean_ground_truth_probability=("ground_truth_probability", "mean"),
            observations=(target_column, "size"),
        )
        .sort_values("offered_rate")
    )
    return grouped
