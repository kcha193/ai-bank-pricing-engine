"""Deterministic decision-support wording, templated from backend scenario numbers.

No LLM, and no claim the backend rows cannot support: the comparison rows carry rates and a
modelled objective, not volumes, so the comparison text never talks about volume. Only the joint
scenario, which has no comparison set, restates the selected run's own backend volumes.

Every sentence names the side it is about (home-loan or term-deposit rate), and the leader is
the product's own: lowest for mortgages, highest for deposits.
"""

from __future__ import annotations

from balance_pricing_engine.dashboard.formatting import fmt_money_m, fmt_rate_fraction
from balance_pricing_engine.dashboard.models import (
    SCENARIO_JOINT,
    SCENARIO_TERM_DEPOSIT,
    ScenarioView,
)
from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps

DISCLAIMER = "Decision support only — this dashboard never changes a bank price."


def _gap(bps: float, reference: str) -> str:
    if bps == 0:
        return f"level with the {reference}"
    direction = "above" if bps > 0 else "below"
    return f"{abs(bps):.0f} bps {direction} the {reference}"


def _joint_text(view: ScenarioView) -> list[str]:
    selected = view.selected
    return [
        "Under the joint competitor assumption, the optimiser's home-loan rate is "
        f"{fmt_rate_fraction(selected.loan_rate)} and its term-deposit rate is "
        f"{fmt_rate_fraction(selected.deposit_rate)}, for a modelled objective of "
        f"{fmt_money_m(selected.objective)}.",
        f"Expected lending volume is {fmt_money_m(selected.expected_loan_volume)} against "
        f"expected deposits of {fmt_money_m(selected.expected_deposit_volume)}; the funding "
        f"shortfall is {fmt_money_m(selected.funding_shortfall)}. A higher deposit rate would "
        "attract more deposits but raise funding cost.",
    ]


def build_decision_text(view: ScenarioView) -> str:
    if view.mode == SCENARIO_JOINT:
        return "\n\n".join([*_joint_text(view), DISCLAIMER])
    is_deposit = view.mode == SCENARIO_TERM_DEPOSIT
    side = "term-deposit" if is_deposit else "home-loan"
    leader_name = f"market leader ({'highest' if is_deposit else 'lowest'} rate)"
    paragraphs = []
    position = view.position
    if position is not None:
        paragraphs.append(
            f"Current {side} pricing is {_gap(position.bps_vs_best, leader_name)} and "
            f"{_gap(position.bps_vs_median, 'market median')} "
            f"(rank #{position.rank} of {position.bank_count})."
        )
    by_label = {row.label: row for row in view.rows}
    median, leader = by_label.get("Match median"), by_label.get("Match leader")
    if median is not None and leader is not None:
        paragraphs.append(
            f"If competitors price their {side} rate at the {leader_name} rather than the "
            "median, the modelled objective changes by "
            f"{fmt_money_m(leader.objective - median.objective, signed=True)} and the "
            f"optimiser's {side} rate moves from {fmt_rate_fraction(median.our_rate)} to "
            f"{fmt_rate_fraction(leader.our_rate)}."
        )
    if view.model_optimum_rate is not None:
        paragraphs.append(
            f"Under current assumptions, the model optimum {side} rate is "
            f"{fmt_rate_fraction(view.model_optimum_rate)}."
        )
    if is_deposit:
        paragraphs.append(
            "A higher deposit rate attracts depositors but increases the bank's funding cost; "
            "the most attractive rate for depositors is not the best rate for bank margin."
        )
    paragraphs.append(DISCLAIMER)
    return "\n\n".join(paragraphs)


#: Backend row label -> what the console calls it. "No change" is the optimiser's answer under
#: an unchanged competitor assumption, not the bank's advertised rate (which it never reads).
DISPLAY_LABELS = {"No change": "No-change optimum"}
BASELINE_LABEL = "Match median"


def scenario_differences(view: ScenarioView) -> list[dict[str, str]]:
    """Each scenario against the Match-median run, as display strings.

    Differences are of the optimiser's own outputs (objective dollars, modelled rate); they are
    deterministic restatements, not new economics. Returns ``[]`` without a baseline row.
    """
    baseline = next((row for row in view.rows if row.label == BASELINE_LABEL), None)
    if baseline is None:
        return []
    return [
        {
            "Scenario": DISPLAY_LABELS.get(row.label, row.label),
            "Our rate": fmt_rate_fraction(row.our_rate),
            "Rate vs Match median": (
                f"{rate_change_bps(baseline.our_rate * 100, row.our_rate * 100):+.0f} bps"
                if row.our_rate != baseline.our_rate
                else "same"
            ),
            "Objective vs Match median": (
                fmt_money_m(row.objective - baseline.objective, signed=True)
                if row.objective != baseline.objective
                else "same"
            ),
        }
        for row in view.rows
    ]
