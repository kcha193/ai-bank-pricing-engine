"""Deterministic decision-support wording, templated from backend scenario numbers.

No LLM, and no claim the backend rows cannot support: the comparison rows carry rates and a
modelled objective, not volumes, so the text never talks about volume.
"""

from __future__ import annotations

from balance_pricing_engine.dashboard.formatting import fmt_money_m, fmt_rate_fraction
from balance_pricing_engine.dashboard.models import ScenarioView
from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps

DISCLAIMER = "Decision support only — this dashboard never changes a bank price."


def _gap(bps: float, reference: str) -> str:
    if bps == 0:
        return f"level with the {reference}"
    direction = "above" if bps > 0 else "below"
    return f"{abs(bps):.0f} bps {direction} the {reference}"


def build_decision_text(view: ScenarioView) -> str:
    paragraphs = []
    position = view.position
    if position is not None:
        paragraphs.append(
            f"Current pricing is {_gap(position.bps_vs_best, 'market leader')} and "
            f"{_gap(position.bps_vs_median, 'market median')} "
            f"(rank #{position.rank} of {position.bank_count})."
        )
    by_label = {row.label: row for row in view.rows}
    median, leader = by_label.get("Match median"), by_label.get("Match leader")
    if median is not None and leader is not None:
        paragraphs.append(
            "If competitors price at the market leader rather than the median, the modelled "
            f"objective changes by {fmt_money_m(leader.objective - median.objective, signed=True)} "
            f"and the optimiser's rate moves from {fmt_rate_fraction(median.our_rate)} to "
            f"{fmt_rate_fraction(leader.our_rate)}."
        )
    if view.model_optimum_rate is not None:
        paragraphs.append(
            "Under current assumptions, the model optimum is "
            f"{fmt_rate_fraction(view.model_optimum_rate)}."
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
