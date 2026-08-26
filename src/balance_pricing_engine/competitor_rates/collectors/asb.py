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
    #: Verified live 2026-08-25: 11 rates, specials 4.79/4.99/5.35 at 6/12/18m.
    #: Requires --render-javascript; the rate table is client-rendered.
    live_verified: ClassVar[bool] = True

    special_product_markers: ClassVar[tuple[str, ...]] = ("special",)
    standard_product_markers: ClassVar[tuple[str, ...]] = ("standard",)
    min_equity_product_markers: ClassVar[tuple[str, ...]] = ("special",)
    # Verified live 2026-08-25: ASB's fixed table emits bare rows such as
    # "6 month | 4.79% p.a. | Interest Rate" with no tier word, and those rates
    # sit at the special tier (4.79 at 6m against ANZ's 5.39 carded).
    default_fixed_category: ClassVar[str] = "special"
    # ASB's unmarked row *is* the special tier, so it carries the same 20%
    # equity condition "special" would have implied via min_equity_product_markers.
    default_fixed_category_implies_min_equity: ClassVar[bool] = True
    excluded_product_markers: ClassVar[tuple[str, ...]] = (
        "top up",
        "top-up",
        "closed to new application",
        "back my build",
        "better homes",
    )
