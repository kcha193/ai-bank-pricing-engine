"""Westpac New Zealand public rate collector."""

from __future__ import annotations

from typing import ClassVar

from balance_pricing_engine.competitor_rates.collectors.base import BankPricingCollector


class WestpacCollector(BankPricingCollector):
    """Westpac advertises "Special" fixed rates and "Choices" floating products.

    Westpac specials require 20% equity and a Choices Everyday transactional
    account, which the shared package-requirement parser records as evidence.
    """

    bank: ClassVar[str] = "Westpac"
    home_loan_url: ClassVar[str] = "https://www.westpac.co.nz/home-loans-mortgages/interest-rates/"
    term_deposit_url: ClassVar[str] = (
        "https://www.westpac.co.nz/kiwisaver-investments/term-deposits-pie-investments/"
        "term-deposit-pie-investment-interest-rates/"
    )
    live_verified: ClassVar[bool] = False

    special_product_markers: ClassVar[tuple[str, ...]] = ("special",)
    standard_product_markers: ClassVar[tuple[str, ...]] = ("standard", "choices")
    min_equity_product_markers: ClassVar[tuple[str, ...]] = ("special",)
