"""Rule-backed config anomaly detection. No LLM required."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AnomalyReport:
    rule_id: str
    severity: str  # "warning" | "error"
    field_path: str
    observed_value: Any
    message: str


class ConfigAnomalyChecker:
    """Check an EngineConfig for known risky or inconsistent configurations."""

    def check(self, config: Any) -> list[AnomalyReport]:
        """Run all rules and return a list of AnomalyReports."""
        reports: list[AnomalyReport] = []
        reports.extend(self._check_negative_wholesale_spread(config))
        reports.extend(self._check_penalty_dominated(config))
        reports.extend(self._check_small_customer_pool(config))
        reports.extend(self._check_narrow_spread_band(config))
        reports.extend(self._check_competitor_rates_non_strict(config))
        return reports

    def _check_negative_wholesale_spread(self, config: Any) -> list[AnomalyReport]:
        spread = getattr(getattr(config, "market", None), "wholesale_funding_spread", None)
        if spread is not None and spread < 0:
            return [
                AnomalyReport(
                    rule_id="negative_wholesale_spread",
                    severity="warning",
                    field_path="market.wholesale_funding_spread",
                    observed_value=spread,
                    message=(
                        f"wholesale_funding_spread={spread:.4f} is negative. "
                        "This means the engine is modelling a below-cash-rate funding benefit, "
                        "which is unusual. Verify this is intentional."
                    ),
                )
            ]
        return []

    def _check_penalty_dominated(self, config: Any) -> list[AnomalyReport]:
        spread = getattr(getattr(config, "market", None), "wholesale_funding_spread", None)
        penalty = getattr(
            getattr(config, "balance_sheet", None), "funding_shortfall_penalty_rate", None
        )
        if spread is not None and penalty is not None and spread > 0 and penalty < spread * 0.1:
            return [
                AnomalyReport(
                    rule_id="penalty_dominated_by_funding_spread",
                    severity="warning",
                    field_path="balance_sheet.funding_shortfall_penalty_rate",
                    observed_value=penalty,
                    message=(
                        f"funding_shortfall_penalty_rate={penalty:.4f} is less than 10% of "
                        f"wholesale_funding_spread={spread:.4f}. The shortfall penalty may be "
                        "too weak to meaningfully steer deposit funding behaviour."
                    ),
                )
            ]
        return []

    def _check_small_customer_pool(self, config: Any) -> list[AnomalyReport]:
        reports: list[AnomalyReport] = []
        sim = getattr(config, "simulation", None)
        if sim is None:
            return []
        for field, attr in [("n_loan_customers", "loan"), ("n_deposit_customers", "deposit")]:
            n = getattr(sim, field, None)
            if n is not None and n < 1000:
                reports.append(
                    AnomalyReport(
                        rule_id="small_customer_pool",
                        severity="warning",
                        field_path=f"simulation.{field}",
                        observed_value=n,
                        message=(
                            f"simulation.{field}={n} is below 1000. Small pools increase "
                            f"Monte Carlo variance in {attr} volume estimates and may "
                            "destabilise sensitivity analysis."
                        ),
                    )
                )
        return reports

    def _check_narrow_spread_band(self, config: Any) -> list[AnomalyReport]:
        reports: list[AnomalyReport] = []
        ps = getattr(config, "pricing_strategy", None)
        if ps is None:
            return []
        for field, label in [
            ("home_loan_spread_bps", "home loan"),
            ("term_deposit_spread_bps", "term deposit"),
        ]:
            band = getattr(ps, field, None)
            if band is None:
                continue
            width_bps = getattr(band, "max", 0) - getattr(band, "min", 0)
            if width_bps < 50:
                reports.append(
                    AnomalyReport(
                        rule_id="narrow_spread_band",
                        severity="warning",
                        field_path=f"pricing_strategy.{field}",
                        observed_value=width_bps,
                        message=(
                            f"pricing_strategy.{field} band width={width_bps} bps is below "
                            f"50 bps. A narrow {label} spread band risks hitting boundary "
                            "limits and may require boundary expansion."
                        ),
                    )
                )
        return reports

    def _check_competitor_rates_non_strict(self, config: Any) -> list[AnomalyReport]:
        cr = getattr(config, "competitor_rates", None)
        if cr is None:
            return []
        if getattr(cr, "enabled", False) and not getattr(cr, "strict", True):
            return [
                AnomalyReport(
                    rule_id="competitor_rates_non_strict",
                    severity="warning",
                    field_path="competitor_rates.strict",
                    observed_value=False,
                    message=(
                        "competitor_rates.enabled=True but strict=False. "
                        "Validation failures will warn and continue rather than abort. "
                        "Consider strict=True in production runs."
                    ),
                )
            ]
        return []
