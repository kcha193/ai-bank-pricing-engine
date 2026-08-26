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
    #: Verified live 2026-08-25: 17 rates, specials 4.75/4.99/5.35 at 6/12/18m.
    #: Requires --render-javascript; the rate table is client-rendered.
    live_verified: ClassVar[bool] = True

    special_product_markers: ClassVar[tuple[str, ...]] = ("special",)
    standard_product_markers: ClassVar[tuple[str, ...]] = ("standard", "choices")
    min_equity_product_markers: ClassVar[tuple[str, ...]] = ("special",)
    # Verified live 2026-08-25: Westpac marks its specials in-row ("1 year -
    # special *"), so an unmarked fixed row is the carded rate. Opposite of
    # ASB/BNZ -- which is why this cannot be inferred generically.
    default_fixed_category: ClassVar[str] = "standard"
