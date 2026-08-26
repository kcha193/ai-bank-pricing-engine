"""ANZ New Zealand public rate collector."""

from __future__ import annotations

from typing import ClassVar

from balance_pricing_engine.competitor_rates.collectors.base import BankPricingCollector


class ANZCollector(BankPricingCollector):
    """ANZ advertises "Special" fixed rates alongside carded "Standard" rates.

    ANZ specials are conditional on at least 20% equity, which the rate table
    footnotes rather than restating on every row.
    """

    bank: ClassVar[str] = "ANZ"
    home_loan_url: ClassVar[str] = "https://www.anz.co.nz/rates-fees-agreements/home-loans/"
    term_deposit_url: ClassVar[str] = "https://www.anz.co.nz/rates-fees-agreements/term-deposits/"
    #: Verified live 2026-08-25: 15 rates, specials 4.79/4.99/5.45/5.49/5.59 at 6/12/18/24/36m.
    #: Requires --render-javascript; the rate table is client-rendered.
    live_verified: ClassVar[bool] = True

    special_product_markers: ClassVar[tuple[str, ...]] = ("special",)
    standard_product_markers: ClassVar[tuple[str, ...]] = ("standard", "carded")
    min_equity_product_markers: ClassVar[tuple[str, ...]] = ("special",)
    # ANZ labels both tiers in-row, so no unmarked-row default is needed.
    excluded_product_markers: ClassVar[tuple[str, ...]] = (
        "top up",
        "top-up",
        "closed to new application",
        "good energy",
        "tideover",
    )
