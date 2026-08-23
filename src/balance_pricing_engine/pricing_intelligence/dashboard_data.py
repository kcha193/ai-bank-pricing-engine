"""Presentation-ready frames for the market-intelligence dashboard section.

Kept free of Streamlit so the shaping logic is unit-testable on its own and the
dashboard module stays a thin rendering layer. Everything here returns pandas
objects or plain dicts.
"""

from __future__ import annotations

from typing import Optional, Sequence

import pandas as pd

from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord
from balance_pricing_engine.pricing_intelligence.changes import RateChange
from balance_pricing_engine.pricing_intelligence.snapshot import MarketSnapshot, _term_label


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


def build_history_series(
    records: Sequence[CompetitorRateRecord],
    term_months: Optional[int] = None,
    withdrawn_dates: Optional[dict] = None,
) -> pd.DataFrame:
    """Wide date-by-bank frame of advertised rates, ready to line-chart.

    ``withdrawn_dates`` maps a bank to the dates it was confirmed withdrawn at
    this term (see :class:`RateChange`, ``change_type is ChangeType.REMOVED_PRODUCT``).
    The observation history itself has no terminating row for a withdrawal, so
    without this the ``ffill()`` below would carry the bank's last advertised
    rate through every later chart date -- introduced by other banks'
    changes -- straight past the point it actually stopped being offered. A
    bank that later returns only has the gap between withdrawal and its next
    real observation blanked, not every date after the withdrawal.
    """
    frame = build_history_frame(records)
    if frame.empty:
        return frame
    if term_months is not None:
        frame = frame[frame["Term"] == term_months]
    if frame.empty:
        return pd.DataFrame()
    # A bank can reprice the same term more than once on one UTC date. Keep only
    # the chronologically last observation per (Date, Bank) so a same-day
    # increase isn't charted as the day's lowest rate.
    frame = frame.drop_duplicates(subset=["Date", "Bank"], keep="last")
    pivoted = frame.pivot_table(index="Date", columns="Bank", values="Rate (%)").ffill()

    for bank, dates in (withdrawn_dates or {}).items():
        if bank not in pivoted.columns:
            continue
        real_dates = sorted(frame.loc[frame["Bank"] == bank, "Date"])
        for withdrawn_on in dates:
            gap = pivoted.index > withdrawn_on
            next_real = next((d for d in real_dates if d > withdrawn_on), None)
            if next_real is not None:
                gap &= pivoted.index < next_real
            pivoted.loc[gap, bank] = None

    return pivoted
