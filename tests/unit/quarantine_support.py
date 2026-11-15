"""Deterministic, offline market harness for the quarantine-governance tests.

Scripted collectors replace live fetching; every run uses a fixed business
clock (``T0 + day``), an in-memory (or temporary) SQLite store and a recording
alert sink, so nothing here touches the network, a production database or a
real alert channel.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from balance_pricing_engine.competitor_rates.collectors import (
    BankCollectionResult,
    CollectionStatus,
)
from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType
from balance_pricing_engine.pricing_intelligence.alerts import Alert
from balance_pricing_engine.pricing_intelligence.config import (
    CollectionConfig,
    PricingIntelligenceConfig,
)
from balance_pricing_engine.pricing_intelligence.orchestration import (
    OperationalRunResult,
    run_operational_cycle,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore
from balance_pricing_engine.pricing_intelligence.strategy import resolve_competitor_rate

T0 = datetime(2026, 11, 10, 18, 30, tzinfo=timezone.utc)
BANKS = ["ANZ", "ASB", "BNZ", "Westpac", "Kiwibank"]
TD = ProductType.TERM_DEPOSIT
HL = ProductType.HOME_LOAN

#: Term-deposit peer rates mirroring tests/fixtures (ASB/ANZ/Westpac 4.00, BNZ/Kiwibank 4.05).
TD_DEFAULT_RATES = {"ANZ": 4.00, "ASB": 4.00, "BNZ": 4.05, "Westpac": 4.00, "Kiwibank": 4.05}
HL_DEFAULT_RATES = {"ANZ": 5.10, "ASB": 5.05, "BNZ": 5.15, "Westpac": 5.10, "Kiwibank": 5.20}


def at(day: int, hours: float = 0.0) -> datetime:
    return T0 + timedelta(days=day, hours=hours)


def make_rate(
    product: ProductType, bank: str, term: int, rate: float, captured_at: datetime, **extra: Any
) -> CompetitorRateRecord:
    deposit = product is TD
    payload: dict[str, Any] = {
        "bank": bank,
        "product_type": product,
        "product_name": "Term Deposit" if deposit else "Fixed Special",
        "rate_category": "term_deposit" if deposit else "special",
        "term_label_raw": f"{term} months",
        "term_months_normalised": term,
        "advertised_rate_pct": rate,
        "source_url": f"https://rates.example.test/{bank.lower()}/{product.value}",
        "captured_at_utc": captured_at,
        "raw_text_context": f"{bank} {term} months {rate}%",
        "raw_html_sha256": "a" * 64,
    }
    payload.update(extra)
    return CompetitorRateRecord(**payload)


class ScriptedCollector:
    """A bank collector whose page content the test controls.

    ``rates`` maps term -> rate, or term -> ``{"rate": x, <record overrides>}`` for a
    reading that also changes eligibility or promotion fields.
    """

    def __init__(self, bank: str, rates: dict[int, Any]) -> None:
        self.bank = bank
        self.rates = rates
        self.fail = False
        self.home_loan_url = f"https://rates.example.test/{bank.lower()}/home_loan"
        self.term_deposit_url = f"https://rates.example.test/{bank.lower()}/term_deposit"

    def _collect(self, product: ProductType, captured_at_utc: datetime) -> BankCollectionResult:
        url = self.term_deposit_url if product is TD else self.home_loan_url
        if self.fail:
            return BankCollectionResult(
                bank=self.bank,
                product_type=product,
                status=CollectionStatus.FAILED,
                source_url=url,
                error="scripted outage",
                error_type="ConnectionError",
            )
        records = []
        for term, spec in self.rates.items():
            extra = dict(spec) if isinstance(spec, dict) else {}
            rate = extra.pop("rate") if isinstance(spec, dict) else spec
            records.append(make_rate(product, self.bank, term, rate, captured_at_utc, **extra))
        return BankCollectionResult(
            bank=self.bank,
            product_type=product,
            status=CollectionStatus.SUCCESS,
            source_url=url,
            records=tuple(records),
        )

    def collect_mortgage_rates(self, captured_at_utc=None, persist_snapshot=False):
        return self._collect(HL, captured_at_utc)

    def collect_term_deposit_rates(self, captured_at_utc=None, persist_snapshot=False):
        return self._collect(TD, captured_at_utc)


class RecordingSink:
    """Alert sink that records deliveries instead of sending anything."""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.alerts.append(alert)

    def titles(self) -> list[str]:
        return [alert.title for alert in self.alerts]


class Market:
    """One product's scripted market on a (possibly shared) observation store."""

    def __init__(
        self,
        product: ProductType = TD,
        *,
        terms: tuple[int, ...] = (12,),
        rates: Optional[dict[str, float]] = None,
        store: Optional[ObservationStore] = None,
        **config: Any,
    ) -> None:
        self.product = product
        defaults = TD_DEFAULT_RATES if product is TD else HL_DEFAULT_RATES
        base = dict(rates or defaults)
        self.config = PricingIntelligenceConfig(
            selected_banks=list(BANKS),
            tracked_terms_months=list(terms),
            collection=CollectionConfig(inter_request_delay_seconds=0, max_retries=0),
            **config,
        )
        self.store = store or ObservationStore(":memory:")
        self.collectors = {
            bank: ScriptedCollector(bank, {term: base[bank] for term in terms}) for bank in BANKS
        }
        self.service = PricingIntelligenceService(
            config=self.config, store=self.store, collectors=list(self.collectors.values())
        )
        self.sink = RecordingSink()
        self.now = at(0, 1)

    # ------------------------------------------------------------------ script

    def set(self, bank: str, rate: Any, term: int = 12) -> None:
        self.collectors[bank].rates[term] = rate

    def fail(self, bank: str, failing: bool = True) -> None:
        self.collectors[bank].fail = failing

    def run(self, day: int, run_id: Optional[str] = None) -> OperationalRunResult:
        self.now = max(self.now, at(day, 1))
        return run_operational_cycle(
            self.service,
            product_type=self.product,
            captured_at_utc=at(day),
            run_id=run_id or (f"run-{day}" if self.product is TD else f"hl-run-{day}"),
            alert_sinks={"test": self.sink},
        )

    # ----------------------------------------------------------------- reading

    def snapshot(self, when: datetime, **kwargs: Any):
        return self.service.get_market_snapshot(product_type=self.product, now=when, **kwargs)

    def bank_rate(self, bank: str, when: datetime, term: int = 12, **kwargs: Any):
        market = self.snapshot(when, **kwargs).term(term)
        entries = [entry for entry in (market.entries if market else ()) if entry.bank == bank]
        return entries[0].rate_pct if entries else None

    def leader(self, when: datetime, term: int = 12) -> Optional[float]:
        snapshot = self.snapshot(when)
        return resolve_competitor_rate("MARKET_LEADER", snapshot, term).rate_pct

    def pending(self):
        return self.service.list_review_cases(self.product, now=self.now)

    def only_pending(self):
        cases = self.pending()
        assert len(cases) == 1, cases
        return cases[0]

    def ledger_rows(self) -> list[dict]:
        rows = self.store._connection.execute(
            "SELECT * FROM quarantine_reviews ORDER BY review_seq"
        ).fetchall()
        return [dict(row) for row in rows]


def spike_market(day_spike: int = 2, rate: float = 4.90, **market_kwargs: Any) -> Market:
    """A TD market where ASB's 12m reading jumps to ``rate`` on ``day_spike`` (Q7 shape)."""
    market = Market(TD, **market_kwargs)
    for day in range(day_spike):
        market.run(day)
    market.set("ASB", rate)
    market.run(day_spike)
    return market
