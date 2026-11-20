"""Apply validated competitor rates to existing engine configuration."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
from pathlib import Path
from statistics import median
from typing import Iterable, Optional, Sequence

from pydantic import ValidationError

from balance_pricing_engine.competitor_rates.collectors import (
    COLLECTORS_BY_BANK,
    BankPricingCollector,
)
from balance_pricing_engine.competitor_rates.models import (
    CompetitorRateRecord,
    CompetitorRatesConfig,
    ProductType,
    ResolvedCompetitorRates,
)
from balance_pricing_engine.competitor_rates.store import load_rate_run
from balance_pricing_engine.competitor_rates.validate import validate_staleness
from balance_pricing_engine.config import revalidation_payload
from balance_pricing_engine.pricing_intelligence.config import PricingIntelligenceConfig
from balance_pricing_engine.pricing_intelligence.coverage import product_phrase
from balance_pricing_engine.pricing_intelligence.deposit_specials import (
    SPECIAL_CATEGORY,
    DepositSpecialAssessment,
    SpecialComparability,
    assess_deposit_specials,
    comparable_keys,
)

logger = logging.getLogger(__name__)

#: Strategies that summarise a market and so need distinct-bank coverage (DATA-003).
MARKET_STRATEGIES = frozenset({"market_average", "market_min", "market_max", "market_median"})

#: ``validation_status`` values the trusted pipeline gives a quarantined/suspicious reading
#: ("warning") or a rejected one ("failed"). Neither may feed a market statistic.
UNTRUSTED_VALIDATION_STATUSES = frozenset({"warning", "failed"})

#: Trust evidence a legacy ``latest.json`` cannot carry. Disclosed on every resolution, never
#: invented: these controls exist only on the pricing-intelligence (SQLite) pipeline.
LEGACY_REDUCED_ASSURANCE = (
    "No operator quarantine review (approve/reject/void) is recorded in a legacy "
    "competitor_rates source; its readings have not been through quarantine.",
    "No peer-plausibility assessment is recorded for legacy competitor_rates readings.",
    "No operational collection ledger: source age is the file's single capture time, "
    "not each bank's latest collection attempt.",
)


class CompetitorRateResolutionError(ValueError):
    """A strict competitor-rate source could not be resolved (stale, failed
    validation, or an unresolvable strategy). Distinguishes competitor-rate-specific
    validation failures from unrelated ``ValueError``s elsewhere in the pricing
    pipeline, so callers (e.g. the API layer) can map only this failure to a
    competitor-rate-specific error response."""


def apply_competitor_rates_to_config(config):
    """Return a config with competitor rates applied, or the original config when disabled.

    A caller that must act on the source's warnings (staleness, uncertain specials) uses
    :func:`apply_competitor_rates_with_resolution`, which also returns them. This wrapper logs
    them at WARNING level so they are never silently dropped.
    """
    applied, _resolution = apply_competitor_rates_with_resolution(config)
    return applied


def apply_competitor_rates_with_resolution(
    config, *, now: Optional[datetime] = None
) -> tuple[object, Optional[ResolvedCompetitorRates]]:
    """Apply competitor rates and return the resolution evidence alongside the config.

    The overlay changes only the four numeric market fields; warning text and provenance
    live on the returned :class:`ResolvedCompetitorRates`, never in the configuration, so
    the effective-config hash is unaffected. Disabled: the same config object and ``None``.
    """
    competitor_config = getattr(config, "competitor_rates", None)
    if competitor_config is None or not competitor_config.enabled:
        return config, None
    resolved = load_and_resolve_competitor_rates(
        competitor_config,
        now=now,
        intelligence_config=getattr(config, "pricing_intelligence", None),
    )
    if resolved.validation_warnings:
        logger.warning(
            "Competitor rates applied from %s (strict=%s, age %.1f h, max %s h, stale=%s) "
            "with warnings: %s",
            resolved.source_file_name,
            resolved.strict,
            resolved.source_age_hours,
            resolved.max_staleness_hours,
            resolved.stale,
            "; ".join(resolved.validation_warnings),
        )
    # home_loan_reference_rate and term_deposit_reference_rate are set to the competitor
    # averages here, so home_loan_spread_bps measures spread-to-competitor rather than
    # spread-to-independent-benchmark after this call.
    applied = revalidate_config(
        config, market_updates={
            "competitor_loan_rate": resolved.mortgage_reference_rate,
            "competitor_deposit_rate": resolved.term_deposit_reference_rate,
            "home_loan_reference_rate": resolved.mortgage_reference_rate,
            "term_deposit_reference_rate": resolved.term_deposit_reference_rate,
        }
    )
    return applied, resolved


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
        data = revalidation_payload(config)
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
    *,
    now: Optional[datetime] = None,
    min_banks: Optional[int] = None,
    intelligence_config: Optional[PricingIntelligenceConfig] = None,
) -> ResolvedCompetitorRates:
    """Load a canonical latest file and resolve scenario-level competitor references.

    The legacy path shares the trusted pipeline's policies rather than keeping its own:

    * the collectors' term-deposit fence (fund, KiwiSaver, PIE effective-rate rows), applied
      again here so a file written before Phase 2d cannot feed them to a benchmark;
    * the Phase 2b term-deposit special comparability policy;
    * the Phase 2c minimum of distinct eligible banks behind a market statistic
      (``min_banks``; ``None`` means ``intelligence_config.min_banks``, whose default is the
      documented policy), in strict and non-strict mode alike;
    * one representative per bank -- the most competitive eligible rate -- before any market
      aggregate (DATA-018).

    Strict mode raises :class:`CompetitorRateResolutionError` for a stale or failed source.
    Non-strict mode resolves, and reports the source's age, threshold, validation status and
    every warning on the result.
    """
    intelligence = intelligence_config or PricingIntelligenceConfig()
    required_banks = intelligence.min_banks if min_banks is None else min_banks
    evaluated_at = now or datetime.now(timezone.utc)
    source_path = Path(competitor_config.source_file)
    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    run = load_rate_run(source_path)
    staleness_issues = validate_staleness(
        run,
        max_staleness_hours=competitor_config.max_staleness_hours,
        strict=competitor_config.strict,
        now=evaluated_at,
    )
    errors = [issue for issue in staleness_issues if issue.severity == "error"]
    if errors:
        raise CompetitorRateResolutionError("; ".join(issue.message for issue in errors))
    if competitor_config.strict and run.validation_summary.status == "failed":
        raise CompetitorRateResolutionError("competitor-rate source file failed validation")

    resolution_warnings: list[str] = []
    trusted = _trusted_records(run.records, competitor_config, resolution_warnings)

    mortgage_records = _filter_mortgage_records(trusted, competitor_config)
    mortgage_strategy = competitor_config.mortgage.strategy
    bank_order = competitor_config.selected_banks
    mortgage_representatives = _contributing_representatives(
        mortgage_records, mortgage_strategy, competitor_config.mortgage.bank, bank_order
    )
    _require_coverage(
        mortgage_representatives, mortgage_strategy, ProductType.HOME_LOAN,
        competitor_config.mortgage.term_months, required_banks,
    )
    mortgage_rate = _resolve_strategy(
        mortgage_records,
        strategy=mortgage_strategy,
        bank=competitor_config.mortgage.bank,
        bank_order=bank_order,
    )

    term_deposit_records, assessments = _term_deposit_selection(
        trusted, competitor_config, intelligence
    )
    resolution_warnings.extend(_uncertain_special_warnings(assessments))
    deposit_strategy = competitor_config.term_deposit.strategy
    deposit_representatives = _contributing_representatives(
        term_deposit_records, deposit_strategy, competitor_config.term_deposit.bank, bank_order
    )
    _require_coverage(
        deposit_representatives, deposit_strategy, ProductType.TERM_DEPOSIT,
        competitor_config.term_deposit.term_months, required_banks,
    )
    term_deposit_rate = _resolve_strategy(
        term_deposit_records,
        strategy=deposit_strategy,
        bank=competitor_config.term_deposit.bank,
        bank_order=bank_order,
    )

    age_hours = (evaluated_at - run.captured_at_utc.astimezone(timezone.utc)).total_seconds()
    age_hours /= 3600
    return ResolvedCompetitorRates(
        mortgage_reference_rate=mortgage_rate / 100,
        term_deposit_reference_rate=term_deposit_rate / 100,
        mortgage_strategy=mortgage_strategy,
        term_deposit_strategy=deposit_strategy,
        mortgage_records=mortgage_records,
        term_deposit_records=term_deposit_records,
        captured_at_utc=run.captured_at_utc,
        validation_warnings=[issue.message for issue in staleness_issues]
        + [issue.message for issue in run.validation_summary.issues]
        + resolution_warnings,
        mortgage_bank_representatives=_bank_representatives(mortgage_records, bank_order),
        mortgage_banks=[record.bank for record in mortgage_representatives],
        term_deposit_banks=[record.bank for record in deposit_representatives],
        source_file_name=source_path.name,
        source_sha256=source_sha256,
        strict=competitor_config.strict,
        evaluated_at_utc=evaluated_at,
        source_age_hours=age_hours,
        max_staleness_hours=competitor_config.max_staleness_hours,
        stale=age_hours > competitor_config.max_staleness_hours,
        source_validation_status=run.validation_summary.status,
        min_banks=required_banks,
        resolution_warnings=resolution_warnings,
        deposit_special_assessments=[assessment.describe() for assessment in assessments],
        reduced_assurance=list(LEGACY_REDUCED_ASSURANCE),
    )


def _trusted_records(
    records: Iterable[CompetitorRateRecord],
    competitor_config: CompetitorRatesConfig,
    warnings: list[str],
) -> list[CompetitorRateRecord]:
    """Drop readings the trusted pipeline marked quarantined or rejected, and say so."""
    kept, excluded = [], []
    for record in records:
        if record.validation_status in UNTRUSTED_VALIDATION_STATUSES:
            excluded.append(record)
        else:
            kept.append(record)
    selected = set(competitor_config.selected_banks)
    relevant = [record for record in excluded if record.bank in selected]
    if relevant:
        detail = ", ".join(
            sorted(
                f"{r.bank} {ProductType(r.product_type).value} "
                f"{r.term_months_normalised}m ({r.validation_status})"
                for r in relevant
            )
        )
        warnings.append(
            f"Excluded {len(relevant)} competitor-rate record(s) with an untrusted "
            f"validation status: {detail}"
        )
    return kept


def _contributing_representatives(
    records: list[CompetitorRateRecord],
    strategy: str,
    bank: Optional[str],
    bank_order: Sequence[str],
) -> list[CompetitorRateRecord]:
    representatives = _bank_representatives(records, bank_order)
    if strategy == "bank_specific":
        return [record for record in representatives if record.bank == bank]
    return representatives


def _require_coverage(
    representatives: list[CompetitorRateRecord],
    strategy: str,
    product_type: ProductType,
    term_months: Optional[int],
    min_banks: int,
) -> None:
    """Refuse a market statistic resting on fewer than ``min_banks`` distinct banks.

    The same rule and wording as ``pricing_intelligence.coverage`` (DATA-003): banks are
    counted after every eligibility rule, one representative each, never by record. No
    records at all keeps its own "No competitor rates matched" error.
    """
    if strategy not in MARKET_STRATEGIES or not representatives:
        return
    if min_banks < 1:
        raise CompetitorRateResolutionError("min_banks must be at least 1")
    count = len({record.bank for record in representatives})
    if count >= min_banks:
        return
    scope = (
        product_phrase(product_type, term_months)
        if term_months is not None
        else f"{product_type.value.replace('_', ' ')} (all terms)"
    )
    raise CompetitorRateResolutionError(
        f"Insufficient competitor coverage for {scope}: {count} eligible bank"
        f"{'' if count == 1 else 's'}; minimum required: {min_banks}."
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
    intelligence_config: Optional[PricingIntelligenceConfig] = None,
) -> list[CompetitorRateRecord]:
    """One eligible term-deposit record per selected bank (see :func:`_term_deposit_selection`)."""
    selected, _assessments = _term_deposit_selection(
        records, competitor_config, intelligence_config or PricingIntelligenceConfig()
    )
    return selected


def _fence_term_deposit_record(record: CompetitorRateRecord) -> CompetitorRateRecord:
    """The collectors' non-deposit fence, so a pre-Phase-2d file is classified like a new one."""
    fence = COLLECTORS_BY_BANK.get(record.bank, BankPricingCollector)
    return fence.enrich_term_deposit_record(record)


def _term_deposit_selection(
    records: Iterable[CompetitorRateRecord],
    competitor_config: CompetitorRatesConfig,
    intelligence: PricingIntelligenceConfig,
) -> tuple[list[CompetitorRateRecord], tuple[DepositSpecialAssessment, ...]]:
    """Eligible term-deposit records, one per selected bank, and the special assessments.

    Rows in the configured category (``term_deposit`` or ``pie_term_deposit``) are eligible;
    ``non_comparable`` and any other category never are. For the ordinary benchmark, a
    ``special`` row is assessed with the Phase 2b policy: only a ``comparable`` special is
    eligible (an ``uncertain`` one is reported by the caller, a ``not_comparable`` one is an
    understood exclusion). A comparable special represents its bank only when it is more
    competitive than the bank's ordinary representative at the same payment frequency; the
    ordinary representative itself is chosen exactly as before (:func:`_select_frequency`).
    """
    deposit_config = competitor_config.term_deposit
    selected_banks = competitor_config.selected_banks
    in_scope = [
        _fence_term_deposit_record(record)
        for record in records
        if ProductType(record.product_type) == ProductType.TERM_DEPOSIT
        and record.bank in selected_banks
        and (
            deposit_config.term_months is None
            or record.term_months_normalised == deposit_config.term_months
        )
    ]
    assessments: tuple[DepositSpecialAssessment, ...] = ()
    admitted: set[str] = set()
    if deposit_config.product_type == "term_deposit":
        policy = intelligence.deposit_specials
        assessments = assess_deposit_specials(
            in_scope,
            restriction_markers=policy.restriction_markers,
            availability_markers=policy.availability_markers,
            balance_tier_edges=intelligence.validation.peer.balance_tier_edges,
        )
        admitted = comparable_keys(assessments)
    selected_by_bank: list[CompetitorRateRecord] = []
    for bank in selected_banks:
        ordinary = [
            r for r in in_scope if r.bank == bank and r.rate_category == deposit_config.product_type
        ]
        specials = [
            r
            for r in in_scope
            if r.bank == bank
            and r.rate_category == SPECIAL_CATEGORY
            and r.observation_key() in admitted
        ]
        representative = _deposit_representative(
            ordinary, specials, deposit_config.interest_payment_frequency_preference
        )
        if representative is not None:
            selected_by_bank.append(representative)
    return selected_by_bank, assessments


def _deposit_representative(
    ordinary: list[CompetitorRateRecord],
    specials: list[CompetitorRateRecord],
    preference: list[str],
) -> Optional[CompetitorRateRecord]:
    if not ordinary:
        return _select_frequency(specials, preference, highest_rate=True) if specials else None
    representative = _select_frequency(ordinary, preference)
    better = [
        special
        for special in specials
        if special.interest_payment_frequency == representative.interest_payment_frequency
        and special.advertised_rate_pct > representative.advertised_rate_pct
    ]
    if better:
        return min(better, key=lambda r: (-r.advertised_rate_pct, r.observation_key()))
    return representative


def _uncertain_special_warnings(
    assessments: Iterable[DepositSpecialAssessment],
) -> list[str]:
    return [
        f"Term-deposit special excluded as uncertain (degraded, needs review): "
        f"{a.bank} {a.term_months}m {a.rate_pct:.2f}% ({', '.join(a.reasons)})"
        for a in assessments
        if a.status is SpecialComparability.UNCERTAIN
    ]


def _select_frequency(
    records: list[CompetitorRateRecord],
    preference: list[str],
    *,
    highest_rate: bool = False,
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

    For specials-only selection, ``highest_rate=True`` ranks equally preferred
    matches by descending advertised rate before the observation-key tie-break.
    The same ranking applies when no preferred frequency matches. Ordinary
    deposits retain the default observation-key selection.
    """
    def selection_key(record: CompetitorRateRecord) -> tuple[float, str]:
        return (-record.advertised_rate_pct if highest_rate else 0, record.observation_key())

    for frequency in preference:
        matches = [
            record for record in records if frequency in record.interest_payment_frequency.split("+")
        ]
        if not matches:
            continue
        exact_matches = [
            record for record in matches if record.interest_payment_frequency == frequency
        ]
        return min(exact_matches or matches, key=selection_key)
    return min(records, key=selection_key)


def _bank_representatives(
    records: Iterable[CompetitorRateRecord],
    bank_order: Optional[Sequence[str]] = None,
) -> list[CompetitorRateRecord]:
    """The most competitive record per bank (DATA-018).

    The same rule as the trusted market snapshot: lowest rate for home loans, highest for
    term deposits. An exact tie is broken by ``observation_key()``, so the choice depends
    only on the records' content, never on their order.

    Returned in ``bank_order`` (the configured ``selected_banks``), then by name for any
    other bank: a fixed order keeps the floating-point aggregate independent of the order
    the records arrived in.
    """
    by_bank: dict[str, list[CompetitorRateRecord]] = {}
    for record in records:
        by_bank.setdefault(record.bank, []).append(record)
    position = {bank: index for index, bank in enumerate(bank_order or ())}
    representatives = []
    for bank in sorted(by_bank, key=lambda name: (position.get(name, len(position)), name)):
        group = by_bank[bank]
        lower_is_better = ProductType(group[0].product_type) is ProductType.HOME_LOAN
        representatives.append(
            min(
                group,
                key=lambda r: (
                    r.advertised_rate_pct if lower_is_better else -r.advertised_rate_pct,
                    r.observation_key(),
                ),
            )
        )
    return representatives


def _resolve_strategy(
    records: list[CompetitorRateRecord],
    strategy: str,
    bank: str | None,
    *,
    bank_order: Optional[Sequence[str]] = None,
) -> float:
    """Aggregate one representative rate per bank, so every bank carries equal weight.

    A bank advertising several qualifying rows (LVR tiers, package variants) contributes its
    most competitive one only; another row changes the result only if it changes that bank's
    representative. ``bank_specific`` uses the named bank's representative.
    """
    if strategy == "bank_specific":
        if not bank:
            raise CompetitorRateResolutionError("bank_specific competitor-rate strategy requires bank")
        records = [record for record in records if record.bank == bank]
    if not records:
        raise CompetitorRateResolutionError("No competitor rates matched the configured strategy")
    if len({ProductType(record.product_type) for record in records}) > 1:
        raise CompetitorRateResolutionError(
            "Competitor-rate records for one strategy must describe one product"
        )
    rates = [record.advertised_rate_pct for record in _bank_representatives(records, bank_order)]
    if strategy in {"bank_specific", "market_average"}:
        return sum(rates) / len(rates)
    if strategy == "market_min":
        return min(rates)
    if strategy == "market_max":
        return max(rates)
    if strategy == "market_median":
        return float(median(rates))
    raise CompetitorRateResolutionError(f"Unsupported competitor-rate strategy: {strategy}")
