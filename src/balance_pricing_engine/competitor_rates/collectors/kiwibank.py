"""Kiwibank public rate collector."""

from __future__ import annotations

from typing import ClassVar, Mapping

from balance_pricing_engine.competitor_rates.collectors.base import BankPricingCollector


class KiwibankCollector(BankPricingCollector):
    """Kiwibank publishes "Special" fixed rates requiring 20% equity.

    Kiwibank also lists an offset variable product, captured under its own
    product name rather than being folded into the fixed-rate comparison.
    """

    bank: ClassVar[str] = "Kiwibank"
    #: Explicit alternative names accepted in configuration (see ``bank_identity``).
    bank_aliases: ClassVar[tuple[str, ...]] = (
        "Kiwibank Limited",
        "Kiwi Bank",
    )
    home_loan_url: ClassVar[str] = (
        "https://www.kiwibank.co.nz/personal-banking/home-loans/rates-and-fees/"
    )
    term_deposit_url: ClassVar[str] = (
        "https://www.kiwibank.co.nz/personal-banking/investments/rates-and-charges/"
    )
    #: Verified live 2026-08-25: 14 rates, specials 4.75/4.95 at 6/12m.
    #: Requires --render-javascript; the rate table is client-rendered.
    live_verified: ClassVar[bool] = True

    #: Browser readiness (DATA-007): about half the comparable rows seen on every fully
    #: rendered saved snapshot 2026-08-25..2026-11-16 (15 home-loan and 22 term-deposit rows), so a
    #: half-rendered table or a single stray table never passes as ready.
    readiness_min_rate_rows: ClassVar[Mapping[str, int]] = {
        "home_loan": 7,
        "term_deposit": 11,
    }

    special_product_markers: ClassVar[tuple[str, ...]] = ("special",)
    standard_product_markers: ClassVar[tuple[str, ...]] = ("standard",)
    min_equity_product_markers: ClassVar[tuple[str, ...]] = ("special",)
