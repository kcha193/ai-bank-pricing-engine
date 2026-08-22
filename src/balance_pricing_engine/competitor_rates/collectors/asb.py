"""ASB Bank public rate collector."""

from __future__ import annotations

from typing import ClassVar

from balance_pricing_engine.competitor_rates.collectors.base import BankPricingCollector


class ASBCollector(BankPricingCollector):
    """ASB publishes "Special" fixed rates requiring 20% equity.

    ASB also lists "Back My Build", a low-rate construction product that is not
    comparable with standard fixed lending; it is captured with its own product
    name so market statistics can exclude it.
    """

    bank: ClassVar[str] = "ASB"
    home_loan_url: ClassVar[str] = (
        "https://www.asb.co.nz/home-loans-mortgages/interest-rates-fees.html"
    )
    term_deposit_url: ClassVar[str] = (
        "https://www.asb.co.nz/term-investments/term-investment-rates.html"
    )
    live_verified: ClassVar[bool] = False

    special_product_markers: ClassVar[tuple[str, ...]] = ("special",)
    standard_product_markers: ClassVar[tuple[str, ...]] = ("standard",)
    min_equity_product_markers: ClassVar[tuple[str, ...]] = ("special",)
