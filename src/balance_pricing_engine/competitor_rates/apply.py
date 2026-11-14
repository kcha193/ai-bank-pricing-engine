"""Apply validated competitor rates to existing engine configuration."""

from __future__ import annotations

from statistics import median
from typing import Iterable

from pydantic import ValidationError

from balance_pricing_engine.competitor_rates.models import (
    CompetitorRateRecord,
    CompetitorRatesConfig,
    ProductType,
    ResolvedCompetitorRates,
)
from balance_pricing_engine.competitor_rates.store import load_rate_run
from balance_pricing_engine.competitor_rates.validate import validate_staleness


class CompetitorRateResolutionError(ValueError):
    """A strict competitor-rate source could not be resolved (stale, failed
    validation, or an unresolvable strategy). Distinguishes competitor-rate-specific
    validation failures from unrelated ``ValueError``s elsewhere in the pricing
    pipeline, so callers (e.g. the API layer) can map only this failure to a
    competitor-rate-specific error response."""


def apply_competitor_rates_to_config(config):
    """Return a config with competitor rates applied, or the original config when disabled."""
    competitor_config = getattr(config, "competitor_rates", None)
    if competitor_config is None or not competitor_config.enabled:
        return config
    resolved = load_and_resolve_competitor_rates(competitor_config)
    # home_loan_reference_rate and term_deposit_reference_rate are set to the competitor
    # averages here, so home_loan_spread_bps measures spread-to-competitor rather than
    # spread-to-independent-benchmark after this call.
    return revalidate_config(
        config, market_updates={
            "competitor_loan_rate": resolved.mortgage_reference_rate,
            "competitor_deposit_rate": resolved.term_deposit_reference_rate,
            "home_loan_reference_rate": resolved.mortgage_reference_rate,
            "term_deposit_reference_rate": resolved.term_deposit_reference_rate,
        }
    )


def revalidate_config(
    config, context: str = "competitor-rate overlay", *, market_updates: dict | None = None
):
    """Validate an overlay and preserve its domain-specific error contract.

    An overlay can push a previously-valid config outside its constraints
    (e.g. a competitor reference rate that drives the spread floor negative). Without
    this, that only surfaces later as an opaque ``ValueError`` deep in the grid
    builder, mapped to a generic 500 instead of a competitor-rate-specific 422.

    Shared with the pricing-intelligence market-strategy overlay, which has the
    same failure mode; ``context`` names the overlay in the raised error.
    """
    try:
        data = config.model_dump()
        if market_updates is not None:
            data["market"].update(market_updates)
        return type(config).model_validate(data)
    except ValidationError as exc:
        raise CompetitorRateResolutionError(
            f"{context} produced an invalid configuration: {exc}"
        ) from exc


#: Backwards-compatible private alias retained for existing call sites.
_revalidate = revalidate_config


def load_and_resolve_competitor_rates(
    competitor_config: CompetitorRatesConfig,
) -> ResolvedCompetitorRates:
    """Load a canonical latest file and resolve scenario-level competitor references."""
    run = load_rate_run(competitor_config.source_file)
    staleness_issues = validate_staleness(
        run,
        max_staleness_hours=competitor_config.max_staleness_hours,
        strict=competitor_config.strict,
    )
    errors = [issue for issue in staleness_issues if issue.severity == "error"]
    if errors:
        raise CompetitorRateResolutionError("; ".join(issue.message for issue in errors))
    if competitor_config.strict and run.validation_summary.status == "failed":
        raise CompetitorRateResolutionError("competitor-rate source file failed validation")

    mortgage_records = _filter_mortgage_records(run.records, competitor_config)
    term_deposit_records = _filter_term_deposit_records(run.records, competitor_config)
    mortgage_rate = _resolve_strategy(
        mortgage_records,
        strategy=competitor_config.mortgage.strategy,
        bank=competitor_config.mortgage.bank,
    )
    term_deposit_rate = _resolve_strategy(
        term_deposit_records,
        strategy=competitor_config.term_deposit.strategy,
        bank=competitor_config.term_deposit.bank,
    )
    return ResolvedCompetitorRates(
        mortgage_reference_rate=mortgage_rate / 100,
        term_deposit_reference_rate=term_deposit_rate / 100,
        mortgage_strategy=competitor_config.mortgage.strategy,
        term_deposit_strategy=competitor_config.term_deposit.strategy,
        mortgage_records=mortgage_records,
        term_deposit_records=term_deposit_records,
        captured_at_utc=run.captured_at_utc,
        validation_warnings=[issue.message for issue in staleness_issues]
        + [issue.message for issue in run.validation_summary.issues],
    )


def _filter_mortgage_records(
    records: Iterable[CompetitorRateRecord],
    competitor_config: CompetitorRatesConfig,
) -> list[CompetitorRateRecord]:
    mortgage_config = competitor_config.mortgage
    filtered = [
        record
        for record in records
        if ProductType(record.product_type) == ProductType.HOME_LOAN
        # Subsidised/closed products are retained on the record for audit but
        # must never enter a market statistic -- excluded unconditionally,
        # not just when include_specials/require_min_20_percent_equity happen
        # to filter them out, so a 1.00% top-up can't reach market_average,
        # market_min, or market_median and skew competitor_loan_rate.
        and record.rate_category != "non_comparable"
        and record.bank in competitor_config.selected_banks
        and (
            mortgage_config.term_months is None
            or record.term_months_normalised == mortgage_config.term_months
        )
    ]
    if not mortgage_config.include_specials:
        filtered = [record for record in filtered if record.rate_category != "special"]
    if mortgage_config.require_min_20_percent_equity:
        filtered = [
            record for record in filtered if record.lvr_condition == "min_20_percent_equity"
        ]
    return filtered


def _filter_term_deposit_records(
    records: Iterable[CompetitorRateRecord],
    competitor_config: CompetitorRatesConfig,
) -> list[CompetitorRateRecord]:
    deposit_config = competitor_config.term_deposit
    filtered = [
        record
        for record in records
        if ProductType(record.product_type) == ProductType.TERM_DEPOSIT
        and record.bank in competitor_config.selected_banks
        and record.rate_category == deposit_config.product_type
        and (
            deposit_config.term_months is None
            or record.term_months_normalised == deposit_config.term_months
        )
    ]
    selected_by_bank: list[CompetitorRateRecord] = []
    for bank in competitor_config.selected_banks:
        bank_records = [record for record in filtered if record.bank == bank]
        if not bank_records:
            continue
        selected_by_bank.append(_select_frequency(bank_records, deposit_config.interest_payment_frequency_preference))
    return selected_by_bank


def _select_frequency(
    records: list[CompetitorRateRecord],
    preference: list[str],
) -> CompetitorRateRecord:
    """Pick one record per bank by frequency preference, deterministically.

    A combined option (e.g. "at_maturity+monthly", see
    ``normalise_frequency()``) can tie with a plain single-frequency record
    at the same preference rank -- both offer "monthly". Returning whichever
    happened to come first in ``records`` would let arbitrary source-HTML
    ordering decide the bank's representative deposit rate (and therefore
    the market aggregate and pricing assumption it feeds). An exact match is
    a closer fit for the preference than a combined option that merely
    offers it alongside another frequency, so it wins first; any remaining
    tie (including no preference matching at all) is broken by
    ``observation_key()``, which depends only on the record's own content,
    not parse order.
    """
    for frequency in preference:
        matches = [
            record for record in records if frequency in record.interest_payment_frequency.split("+")
        ]
        if not matches:
            continue
        exact_matches = [
            record for record in matches if record.interest_payment_frequency == frequency
        ]
        return min(exact_matches or matches, key=lambda record: record.observation_key())
    return min(records, key=lambda record: record.observation_key())


def _resolve_strategy(
    records: list[CompetitorRateRecord],
    strategy: str,
    bank: str | None,
) -> float:
    if strategy == "bank_specific":
        if not bank:
            raise CompetitorRateResolutionError("bank_specific competitor-rate strategy requires bank")
        records = [record for record in records if record.bank == bank]
    if not records:
        raise CompetitorRateResolutionError("No competitor rates matched the configured strategy")
    rates = [record.advertised_rate_pct for record in records]
    if strategy in {"bank_specific", "market_average"}:
        return sum(rates) / len(rates)
    if strategy == "market_min":
        return min(rates)
    if strategy == "market_max":
        return max(rates)
    if strategy == "market_median":
        return float(median(rates))
    raise CompetitorRateResolutionError(f"Unsupported competitor-rate strategy: {strategy}")
