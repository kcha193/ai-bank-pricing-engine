"""Presentation-ready frames for the market-intelligence dashboard section.

Kept free of Streamlit so the shaping logic is unit-testable on its own and the
dashboard module stays a thin rendering layer. Everything here returns pandas
objects or plain dicts.
"""

from __future__ import annotations

from typing import Mapping, Sequence

import pandas as pd
from datetime import datetime

from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord
from balance_pricing_engine.pricing_intelligence.changes import RateChange
from balance_pricing_engine.pricing_intelligence.snapshot import MarketSnapshot, _term_label


def build_freshness_frame(
    records: Sequence[CompetitorRateRecord], banks: Sequence[str], max_age_hours: float,
    now: datetime, run_ids_by_bank: Mapping[str, Sequence[str]] | None,
    latest_run_id: str | None,
) -> pd.DataFrame:
    """Expose each bank's oldest current confirmation and latest-run coverage.

    This describes recorded observations. It cannot infer an unrecorded failed
    attempt, or label a successful HTTP request as a verified source reading.

    ``run_ids_by_bank`` (from :meth:`.store.ObservationStore.get_current_observation_run_ids`)
    and ``latest_run_id`` (from :meth:`.store.ObservationStore.latest_run_id`)
    decide "not fully confirmed" by run identity, not by comparing
    ``captured_at_utc`` against the latest run's timestamp: two distinct
    runs can share a ``started_at`` value, which a timestamp comparison
    would then read as "the same run".
    """
    if not banks:
        return pd.DataFrame(
            columns=["Oldest confirmation (UTC)", "Age (hours)", "Status"],
            index=pd.Index([], name="Bank"),
        )
    rows = []
    for bank in banks:
        bank_records = [r for r in records if r.bank == bank]
        confirmed = [r for r in bank_records if r.validation_status != "warning"]
        oldest = min((r.captured_at_utc for r in confirmed), default=None)
        age = (now - oldest).total_seconds() / 3600 if oldest is not None else None
        status = "Missing" if age is None else "Stale" if age > max_age_hours else "Fresh"
        bank_run_ids = (run_ids_by_bank or {}).get(bank, [])
        if any(r.validation_status == "warning" for r in bank_records):
            status = "Quarantined readings"
        elif (
            oldest is not None
            and latest_run_id is not None
            and any(run_id != latest_run_id for run_id in bank_run_ids)
        ):
            status += "; not fully confirmed in latest stored run"
        rows.append({"Bank": bank, "Oldest confirmation (UTC)": oldest,
                     "Age (hours)": age, "Status": status})
    return pd.DataFrame(rows).set_index("Bank")



def build_market_grid_frame(snapshot: MarketSnapshot) -> pd.DataFrame:
    """Bank-by-term grid of advertised rates, with market summary rows appended."""
    if not snapshot.terms:
        return pd.DataFrame()

    terms = snapshot.term_months
    columns = [_term_label(term) for term in terms]
    rows = {
        bank: [snapshot[term].rate_for(bank) for term in terms] for bank in snapshot.banks
    }
    frame = pd.DataFrame.from_dict(rows, orient="index", columns=columns)
    frame.index.name = "Bank"

    summary = pd.DataFrame(
        [
            [snapshot[term].best_rate for term in terms],
            [snapshot[term].median_rate for term in terms],
            [snapshot[term].highest_rate for term in terms],
            [snapshot[term].spread_bps for term in terms],
        ],
        index=["Best", "Median", "Highest", "Spread (bps)"],
        columns=columns,
    )
    summary.index.name = "Bank"
    return pd.concat([frame, summary])


def build_term_summary(snapshot: MarketSnapshot, term_months: int) -> dict:
    """Headline statistics for one term, shaped for metric tiles."""
    market = snapshot.term(term_months)
    if market is None:
        return {}
    return {
        "term_months": term_months,
        "best_rate": market.best_rate,
        "median_rate": market.median_rate,
        "highest_rate": market.highest_rate,
        "spread_bps": market.spread_bps,
        "market_leader": ", ".join(market.market_leaders),
        "bank_count": market.bank_count,
    }


def build_ranking_frame(snapshot: MarketSnapshot, term_months: int) -> pd.DataFrame:
    """Ranked bank table for one term."""
    market = snapshot.term(term_months)
    if market is None:
        return pd.DataFrame()
    return pd.DataFrame(
        [
            {
                "Rank": entry.rank,
                "Bank": entry.bank,
                "Rate (%)": entry.rate_pct,
                "Product": entry.product_name,
                "Max LVR": entry.max_lvr,
            }
            for entry in market.ranking()
        ]
    )


def build_changes_frame(changes: Sequence[RateChange]) -> pd.DataFrame:
    """Recent repricing activity, newest first."""
    if not changes:
        return pd.DataFrame()
    return pd.DataFrame(
        [
            {
                "Detected": change.detected_at.date(),
                "Bank": change.bank,
                "Term": (
                    f"{change.term_months}M" if change.term_months is not None else "Floating"
                ),
                "Type": change.change_type.value,
                "From (%)": change.previous_rate_pct,
                "To (%)": change.current_rate_pct,
                "Move (bps)": change.change_bps,
                "Suspicious": change.suspicious,
            }
            for change in changes
        ]
    )


def build_history_frame(records: Sequence[CompetitorRateRecord]) -> pd.DataFrame:
    """Advertised rate history for charting, indexed by observation date."""
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(
        [
            {
                "Date": record.captured_at_utc.date(),
                "Timestamp": record.captured_at_utc,
                "Bank": record.bank,
                "Term": record.term_months_normalised,
                "Category": record.rate_category,
                "Rate (%)": record.advertised_rate_pct,
            }
            for record in records
        ]
    )
    return frame.sort_values("Timestamp").reset_index(drop=True)


def build_history_series(series: Sequence[tuple]) -> pd.DataFrame:
    """Wide date-by-bank frame of each bank's representative rate, ready to
    line-chart.

    ``series`` is :meth:`~.service.PricingIntelligenceService.get_rate_history_series`'s
    output: ``(date, {bank: rate_pct})`` pairs, one per date something
    changed, with the representative-rate selection (multiple simultaneous
    products/LVR tiers per bank) and withdrawal exclusion already resolved
    there against the store -- reusing the same machinery
    ``build_market_snapshot()``/``get_observations_as_of()`` already get
    right, rather than reimplementing either from raw observation rows here.
    A bank absent from a date's dict has no representative rate as of that
    date (withdrawn, or not yet observed) and charts as a gap.
    """
    if not series:
        return pd.DataFrame()
    frame = pd.DataFrame.from_dict(dict(series), orient="index").sort_index()
    frame.index.name = "Date"
    return frame
