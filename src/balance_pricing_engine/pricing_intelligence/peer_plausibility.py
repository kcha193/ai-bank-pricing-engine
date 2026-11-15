"""Peer-market plausibility signal for a moving rate (DATA-016).

``validation._validate_movement`` only looks at how far a rate moved against its own
history, so a plausible but wrong reading under the threshold (a +60 bps special/standard
mix-up) sails through and can become MARKET_LEADER. This module adds an independent view:
how the candidate compares with *comparable peers* and with how those peers moved.

It is a **review signal, never a verdict**: a flagged reading is quarantined for an operator
(see :mod:`.quarantine`), not rejected, and nothing here approves a rate. With too few
comparable peers it abstains and says so -- no median is ever fabricated -- leaving the
other validation controls in force.

Comparable means: same product, rate category, term, balance tier (a band of the advertised
minimum, see ``PeerPlausibilityThresholds.balance_tier_edges``) and eligibility (LVR / equity
for loans); a different bank; observed recently enough; and itself not
quarantined, rejected or voided. A peer is a *bank*: several rows of one bank in a group
count once.

Product direction is not symmetric. The market leader is the highest deposit rate and the
lowest loan rate, so a reading far on that side does the damage when wrong and gets the
tighter bound; the other side uses the looser ``other_side_deviation_bps``.
"""

from __future__ import annotations

from dataclasses import dataclass
import statistics
from typing import NamedTuple, Optional, Sequence

from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType
from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps
from balance_pricing_engine.pricing_intelligence.config import PeerPlausibilityThresholds

STATUS_OK = "ok"
STATUS_FLAGGED = "flagged"
STATUS_INSUFFICIENT = "insufficient_evidence"


@dataclass(frozen=True)
class PeerAssessment:
    """What the peer check concluded for one moving reading."""

    observation_key: str
    bank: str
    status: str
    #: Distinct comparable peer banks found (before the minimum is applied).
    peer_count: int
    peer_median_pct: Optional[float] = None
    peer_median_move_bps: Optional[float] = None
    #: Candidate minus peer median, in bps (signed).
    deviation_bps: Optional[float] = None
    #: Candidate's own move minus the peers' median move, in bps (signed).
    excess_movement_bps: Optional[float] = None
    detail: str = ""


class PeerEntry(NamedTuple):
    """One record of the current collection, as the peer check sees it."""

    record: CompetitorRateRecord
    baseline: Optional[CompetitorRateRecord]
    #: May serve as a peer: accepted, and not itself quarantined this run.
    trusted: bool
    #: Has a movement against its baseline that the movement rule did not already flag.
    candidate: bool


def _band(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.4f}"


def balance_tier(minimum: Optional[float], edges: Sequence[float]) -> int:
    """Balance tier of an advertised minimum: 0 is the retail tier (no minimum counts as 0)."""
    value = minimum or 0.0
    return sum(1 for edge in edges if value >= edge)


def peer_group_key(record: CompetitorRateRecord, tier_edges: Sequence[float] = ()) -> tuple:
    """Records sharing this key are comparable offers (bank and product name excluded)."""
    term = (
        str(record.term_months_normalised)
        if record.term_months_normalised is not None
        else record.term_label_raw.lower()
    )
    return (
        str(ProductType(record.product_type)),
        record.rate_category,
        term,
        balance_tier(record.min_balance_or_deposit, tier_edges),
        record.lvr_condition or "",
        _band(record.max_lvr),
        _band(record.min_equity),
    )


def _leader_side_is_high(record: CompetitorRateRecord) -> bool:
    return ProductType(record.product_type) is ProductType.TERM_DEPOSIT


def _median(values: Sequence[float]) -> float:
    return float(statistics.median(values))


def assess_peers(
    entries: Sequence[PeerEntry],
    previous: Sequence[CompetitorRateRecord],
    config: PeerPlausibilityThresholds,
) -> list[PeerAssessment]:
    """Assess every candidate entry against its comparable peers."""
    if not config.enabled:
        return []

    # Current-collection readings per (group, bank): (rate, move vs own baseline or None).
    edges = config.balance_tier_edges
    in_batch: set[tuple[tuple, str]] = set()
    current: dict[tuple, dict[str, list[tuple[float, Optional[float]]]]] = {}
    for entry in entries:
        group = peer_group_key(entry.record, edges)
        in_batch.add((group, entry.record.bank))
        if not entry.trusted:
            continue
        move = (
            rate_change_bps(entry.baseline.advertised_rate_pct, entry.record.advertised_rate_pct)
            if entry.baseline is not None
            else None
        )
        current.setdefault(group, {}).setdefault(entry.record.bank, []).append(
            (entry.record.advertised_rate_pct, move)
        )
    # A bank absent from this collection still counts, on its last trusted reading, while
    # that reading is recent enough. A bank *present* but quarantined does not.
    carried: dict[tuple, dict[str, list[CompetitorRateRecord]]] = {}
    for record in previous:
        group = peer_group_key(record, edges)
        if (group, record.bank) not in in_batch:
            carried.setdefault(group, {}).setdefault(record.bank, []).append(record)

    assessments: list[PeerAssessment] = []
    for entry in entries:
        if not entry.candidate or entry.baseline is None:
            continue
        record = entry.record
        group = peer_group_key(record, edges)
        rates: dict[str, float] = {}
        moves: list[float] = []
        for bank, readings in current.get(group, {}).items():
            if bank == record.bank:
                continue
            rates[bank] = _median([rate for rate, _ in readings])
            bank_moves = [move for _, move in readings if move is not None]
            if bank_moves:
                moves.append(_median(bank_moves))
        for bank, prior in carried.get(group, {}).items():
            if bank == record.bank or bank in rates:
                continue
            fresh = [
                r.advertised_rate_pct
                for r in prior
                if (record.captured_at_utc - r.captured_at_utc).total_seconds() / 3600
                <= config.max_peer_age_hours
            ]
            if fresh:
                rates[bank] = _median(fresh)

        key = record.observation_key()
        if len(rates) < config.min_peers:
            assessments.append(
                PeerAssessment(
                    key,
                    record.bank,
                    STATUS_INSUFFICIENT,
                    len(rates),
                    detail=(
                        f"{len(rates)} comparable peer(s); at least {config.min_peers} needed"
                    ),
                )
            )
            continue
        median_rate = _median(list(rates.values()))
        if len(moves) < config.min_peers:
            assessments.append(
                PeerAssessment(
                    key,
                    record.bank,
                    STATUS_INSUFFICIENT,
                    len(rates),
                    peer_median_pct=median_rate,
                    detail=(
                        f"only {len(moves)} peer(s) with a prior reading to compare movement; "
                        f"at least {config.min_peers} needed"
                    ),
                )
            )
            continue
        assessments.append(_judge(entry, rates, median_rate, moves, config))
    return assessments


def _judge(
    entry: PeerEntry,
    rates: dict[str, float],
    median_rate: float,
    moves: list[float],
    config: PeerPlausibilityThresholds,
) -> PeerAssessment:
    record = entry.record
    baseline = entry.baseline
    assert baseline is not None  # candidates always have one
    deviation = rate_change_bps(median_rate, record.advertised_rate_pct)
    own_move = rate_change_bps(baseline.advertised_rate_pct, record.advertised_rate_pct)
    peer_move = _median(moves)
    excess = own_move - peer_move
    spread = _median([abs(rate_change_bps(median_rate, rate)) for rate in rates.values()])
    on_leader_side = (deviation > 0) == _leader_side_is_high(record)
    bound = config.leader_side_deviation_bps if on_leader_side else config.other_side_deviation_bps
    dispersion_bound = config.dispersion_multiple * max(spread, config.dispersion_floor_bps)
    flagged = (
        abs(deviation) >= bound
        and abs(deviation) >= dispersion_bound
        and abs(excess) >= config.movement_excess_bps
        and (excess > 0) == (deviation > 0)  # moving away from the peers, not toward them
    )
    side = "leader side" if on_leader_side else "other side"
    detail = (
        f"{record.advertised_rate_pct:.2f}% vs peer median {median_rate:.3f}% across "
        f"{len(rates)} banks ({deviation:+.0f} bps, {side}); moved {own_move:+.0f} bps while "
        f"peers moved {peer_move:+.0f} bps ({excess:+.0f} bps relative)"
    )
    return PeerAssessment(
        observation_key=record.observation_key(),
        bank=record.bank,
        status=STATUS_FLAGGED if flagged else STATUS_OK,
        peer_count=len(rates),
        peer_median_pct=median_rate,
        peer_median_move_bps=peer_move,
        deviation_bps=deviation,
        excess_movement_bps=excess,
        detail=detail,
    )
