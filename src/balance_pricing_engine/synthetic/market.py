"""Market and competitor rate assumptions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketState:
    cash_rate: float
    wholesale_funding_spread: float
    competitor_loan_rate: float
    competitor_deposit_rate: float
    home_loan_reference_rate: float | None = None
    term_deposit_reference_rate: float | None = None

    @property
    def wholesale_funding_rate(self) -> float:
        """Wholesale funding rate used when deposits do not fully fund lending."""
        return self.cash_rate + self.wholesale_funding_spread
