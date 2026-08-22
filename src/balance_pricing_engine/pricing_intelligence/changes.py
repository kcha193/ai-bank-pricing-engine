"""Detection of material competitor pricing changes.

An *observation* is what a bank advertised at a point in time. A *change* is a
material difference between two observations of the same rate line. The two are
deliberately separate: a daily collection where nothing moved produces plenty of
observations and zero changes.

Every basis-point figure here comes from :mod:`.bps`, so movements are exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable, Optional, Sequence

from balance_pricing_engine.competitor_rates.models import CompetitorRateRecord, ProductType
from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps
from balance_pricing_engine.pricing_intelligence.config import ValidationThresholds


class ChangeType(str, Enum):
    RATE_INCREASE = "RATE_INCREASE"
    RATE_DECREASE = "RATE_DECREASE"
    NEW_PRODUCT = "NEW_PRODUCT"
    REMOVED_PRODUCT = "REMOVED_PRODUCT"
    ELIGIBILITY_CHANGE = "ELIGIBILITY_CHANGE"
    PROMOTION_CHANGE = "PROMOTION_CHANGE"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class RateChange:
    """One structured market pricing change event."""

    observation_key: str
    bank: str
    product_type: ProductType
    product_name: str
    rate_category: str
    term_months: Optional[int]
    change_type: ChangeType
    detected_at: datetime
    previous_rate_pct: Optional[float] = None
    current_rate_pct: Optional[float] = None
    change_bps: Optional[float] = None
    previous_observed_at: Optional[datetime] = None
    #: True when the movement is large enough that a parser fault or a
    #: special/standard mix-up is more likely than a genuine repricing.
    suspicious: bool = False
    detail: Optional[str] = None
    source_url: Optional[str] = None
    run_id: Optional[str] = None

    @property
    def is_rate_move(self) -> bool:
        return self.change_type in (ChangeType.RATE_INCREASE, ChangeType.RATE_DECREASE)

    @property
    def direction(self) -> str:
        if self.change_bps is None or self.change_bps == 0:
            return "FLAT"
        return "UP" if self.change_bps > 0 else "DOWN"

    def describe(self) -> str:
        """One-line human summary, used by alerts and the CLI."""
        term = f"{self.term_months}m" if self.term_months is not None else self.rate_category
        if self.is_rate_move:
            return (
                f"{self.bank} {term} {self.rate_category}: "
                f"{self.previous_rate_pct:.2f}% -> {self.current_rate_pct:.2f}% "
                f"({self.change_bps:+.0f} bps)"
            )
        if self.change_type is ChangeType.NEW_PRODUCT:
            return f"{self.bank} {term} {self.rate_category}: new at {self.current_rate_pct:.2f}%"
        if self.change_type is ChangeType.REMOVED_PRODUCT:
            return (
                f"{self.bank} {term} {self.rate_category}: withdrawn "
                f"(was {self.previous_rate_pct:.2f}%)"
            )
        return f"{self.bank} {term} {self.rate_category}: {self.change_type.value.lower()}"


# Fields compared for a non-price eligibility change.
_ELIGIBILITY_FIELDS = ("max_lvr", "min_equity", "new_business_only", "package_requirement")
_PROMOTION_FIELDS = ("cashback_text", "promotion_text")


def detect_changes(
    previous: Iterable[CompetitorRateRecord],
    current: Iterable[CompetitorRateRecord],
    detected_at: Optional[datetime] = None,
    thresholds: Optional[ValidationThresholds] = None,
    run_id: Optional[str] = None,
    banks_collected: Optional[Iterable[str]] = None,
) -> list[RateChange]:
    """Compare two sets of observations and return the material changes.

    Records are matched on :meth:`CompetitorRateRecord.observation_key`, so a
    repriced line reads as a rate movement rather than one product vanishing and
    another appearing. Identical observations produce no events.

    ``banks_collected`` names the banks whose pages were successfully read this
    run. Withdrawal events are only emitted for those banks -- otherwise a bank
    being unreachable would report its entire range as withdrawn, which is a
    collection failure masquerading as a market event. Leave it ``None`` only
    when every bank in ``previous`` was definitely collected.
    """
    thresholds = thresholds or ValidationThresholds()
    collected = set(banks_collected) if banks_collected is not None else None
    previous_by_key = {record.observation_key(): record for record in previous}
    current_by_key = {record.observation_key(): record for record in current}
    changes: list[RateChange] = []

    for key, current_record in current_by_key.items():
        previous_record = previous_by_key.get(key)
        if previous_record is None:
            changes.append(
                _build(
                    current_record,
                    ChangeType.NEW_PRODUCT,
                    detected_at,
                    run_id,
                    current_rate_pct=current_record.advertised_rate_pct,
                    detail=f"first observed at {current_record.advertised_rate_pct:.2f}%",
                )
            )
            continue
        changes.extend(
            _compare_records(previous_record, current_record, detected_at, thresholds, run_id)
        )

    for key, previous_record in previous_by_key.items():
        if key in current_by_key:
            continue
        if collected is not None and previous_record.bank not in collected:
            # This bank was not successfully collected, so its absence is
            # unknown, not a withdrawal.
            continue
        changes.append(
            _build(
                previous_record,
                ChangeType.REMOVED_PRODUCT,
                detected_at,
                run_id,
                previous_rate_pct=previous_record.advertised_rate_pct,
                previous_observed_at=previous_record.captured_at_utc,
                detail=(
                    "no longer advertised; last seen at "
                    f"{previous_record.advertised_rate_pct:.2f}%"
                ),
            )
        )

    changes.sort(key=lambda change: (change.bank, change.term_months or 0, change.change_type.value))
    return changes


def _compare_records(
    previous: CompetitorRateRecord,
    current: CompetitorRateRecord,
    detected_at: Optional[datetime],
    thresholds: ValidationThresholds,
    run_id: Optional[str],
) -> list[RateChange]:
    changes: list[RateChange] = []

    movement_bps = rate_change_bps(previous.advertised_rate_pct, current.advertised_rate_pct)
    if movement_bps != 0:
        change_type = (
            ChangeType.RATE_INCREASE if movement_bps > 0 else ChangeType.RATE_DECREASE
        )
        suspicious = abs(movement_bps) > thresholds.suspicious_change_bps
        changes.append(
            _build(
                current,
                change_type,
                detected_at,
                run_id,
                previous_rate_pct=previous.advertised_rate_pct,
                current_rate_pct=current.advertised_rate_pct,
                change_bps=movement_bps,
                previous_observed_at=previous.captured_at_utc,
                suspicious=suspicious,
                detail=(
                    f"movement of {abs(movement_bps):.0f} bps exceeds the "
                    f"{thresholds.suspicious_change_bps:.0f} bps review threshold; "
                    "verify against the source page before treating as a genuine reprice"
                )
                if suspicious
                else None,
            )
        )

    eligibility_diff = _diff_fields(previous, current, _ELIGIBILITY_FIELDS)
    if eligibility_diff:
        changes.append(
            _build(
                current,
                ChangeType.ELIGIBILITY_CHANGE,
                detected_at,
                run_id,
                previous_rate_pct=previous.advertised_rate_pct,
                current_rate_pct=current.advertised_rate_pct,
                previous_observed_at=previous.captured_at_utc,
                detail=eligibility_diff,
            )
        )

    promotion_diff = _diff_fields(previous, current, _PROMOTION_FIELDS)
    if promotion_diff:
        changes.append(
            _build(
                current,
                ChangeType.PROMOTION_CHANGE,
                detected_at,
                run_id,
                previous_rate_pct=previous.advertised_rate_pct,
                current_rate_pct=current.advertised_rate_pct,
                previous_observed_at=previous.captured_at_utc,
                detail=promotion_diff,
            )
        )

    return changes


def _diff_fields(
    previous: CompetitorRateRecord,
    current: CompetitorRateRecord,
    fields: Sequence[str],
) -> Optional[str]:
    parts = [
        f"{name}: {getattr(previous, name)!r} -> {getattr(current, name)!r}"
        for name in fields
        if getattr(previous, name) != getattr(current, name)
    ]
    return "; ".join(parts) if parts else None


def _build(
    record: CompetitorRateRecord,
    change_type: ChangeType,
    detected_at: Optional[datetime],
    run_id: Optional[str],
    **kwargs: object,
) -> RateChange:
    return RateChange(
        observation_key=record.observation_key(),
        bank=record.bank,
        product_type=ProductType(record.product_type),
        product_name=record.product_name,
        rate_category=record.rate_category,
        term_months=record.term_months_normalised,
        change_type=change_type,
        detected_at=detected_at or record.captured_at_utc,
        source_url=record.source_url,
        run_id=run_id,
        **kwargs,  # type: ignore[arg-type]
    )


def load_changes(rows: Iterable[dict]) -> list[RateChange]:
    """Rehydrate stored change rows into :class:`RateChange` objects."""
    changes: list[RateChange] = []
    for row in rows:
        changes.append(
            RateChange(
                observation_key=row["observation_key"],
                bank=row["bank"],
                product_type=ProductType(row["product_type"]),
                product_name=row["product_name"],
                rate_category=row["rate_category"],
                term_months=row["term_months"],
                change_type=ChangeType(row["change_type"]),
                detected_at=_parse(row["detected_at"]),
                previous_rate_pct=row["previous_rate_pct"],
                current_rate_pct=row["current_rate_pct"],
                change_bps=row["change_bps"],
                previous_observed_at=_parse_optional(row["previous_observed_at"]),
                suspicious=bool(row["suspicious"]),
                detail=row["detail"],
                source_url=row["source_url"],
                run_id=row["run_id"] or None,
            )
        )
    return changes


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_optional(value: Optional[str]) -> Optional[datetime]:
    return _parse(value) if value else None
