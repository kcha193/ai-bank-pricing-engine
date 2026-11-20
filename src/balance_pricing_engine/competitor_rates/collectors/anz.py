"""ANZ New Zealand public rate collector."""

from __future__ import annotations

from typing import ClassVar, Mapping

from balance_pricing_engine.competitor_rates.collectors.base import BankPricingCollector


class ANZCollector(BankPricingCollector):
    """ANZ advertises "Special" fixed rates alongside carded "Standard" rates.

    ANZ specials are conditional on at least 20% equity, which the rate table
    footnotes rather than restating on every row.
    """

    bank: ClassVar[str] = "ANZ"
    #: Explicit alternative names accepted in configuration (see ``bank_identity``).
    bank_aliases: ClassVar[tuple[str, ...]] = (
        "ANZ Bank",
        "ANZ Bank New Zealand",
        "ANZ Bank New Zealand Limited",
        "ANZ New Zealand",
    )
    home_loan_url: ClassVar[str] = "https://www.anz.co.nz/rates-fees-agreements/home-loans/"
    term_deposit_url: ClassVar[str] = "https://www.anz.co.nz/rates-fees-agreements/term-deposits/"
    #: Verified live 2026-08-25: 15 rates, specials 4.79/4.99/5.45/5.49/5.59 at 6/12/18/24/36m.
    #: Requires --render-javascript; the rate table is client-rendered.
    live_verified: ClassVar[bool] = True

    #: Browser readiness (DATA-007): about half the comparable rows seen on every fully
    #: rendered saved snapshot 2026-08-25..2026-11-16 (14-16 home-loan and 30 term-deposit rows), so a
    #: half-rendered table or a single stray table never passes as ready.
    readiness_min_rate_rows: ClassVar[Mapping[str, int]] = {
        "home_loan": 7,
        "term_deposit": 15,
    }

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
