"""HTTP behaviour configuration for public bank page collection.

Lives in ``competitor_rates``, not ``pricing_intelligence``: collectors are the
lower-level ingestion package ``pricing_intelligence`` is built on top of (see
AGENTS.md), so this config must not be defined in a package the collectors
would then have to import back up to.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator

DEFAULT_USER_AGENT = (
    "ai-bank-pricing-engine/0.1 (+pricing-intelligence; public advertised rates only)"
)

#: Product keys accepted in :attr:`CollectionConfig.readiness_min_rate_rows` (the
#: ``ProductType`` values; spelled out here so this module stays import-free of models).
READINESS_PRODUCT_KEYS = frozenset({"home_loan", "term_deposit"})

#: Allowance per browser attempt for launching Chromium and opening a page, used only by
#: :func:`estimated_worst_case_run_seconds`. Cold launches measured locally take 1-3 s; this
#: is deliberately generous so the estimate errs towards a longer deadline.
BROWSER_STARTUP_ALLOWANCE_SECONDS = 10.0
#: Allowance for post-collection work inside one run (alert dispatch is capped at 30 s per
#: sink and there are up to three dispatch passes over two sinks), used by the same estimate.
POST_COLLECTION_ALLOWANCE_SECONDS = 180.0


class CollectionConfig(BaseModel):
    """HTTP behaviour for public bank page collection."""

    request_timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    retry_backoff_seconds: float = Field(default=2.0, ge=0)
    # Politeness delay between requests to the same host. Public pages only; this
    # keeps collection well below anything a bank would consider aggressive.
    inter_request_delay_seconds: float = Field(default=1.0, ge=0)
    user_agent: str = DEFAULT_USER_AGENT
    # Browser readiness (DATA-007). After the page "load" event, the rendered DOM is
    # re-read every poll interval until the bank's own parser finds enough comparable rate
    # rows for the requested product (see competitor_rates.readiness), or this many seconds
    # pass. None means "same as request_timeout_seconds", the previous fixed wait.
    browser_readiness_timeout_seconds: Optional[float] = Field(default=None, gt=0)
    browser_readiness_poll_seconds: float = Field(default=0.5, gt=0)
    # Per-bank, per-product override of each collector's evidence-based minimum row count,
    # e.g. {"ASB": {"term_deposit": 8}}. Omitted banks/products keep the collector default.
    readiness_min_rate_rows: dict[str, dict[str, int]] = Field(default_factory=dict)
    # End-to-end deadline for one product-scoped operational collection (DATA-008). The
    # default sits above estimated_worst_case_run_seconds() for five banks (about 1,264 s
    # with these defaults), so it targets genuine hangs, not slow-but-progressing runs.
    collection_run_timeout_seconds: float = Field(default=1800.0, gt=0)

    @field_validator("readiness_min_rate_rows")
    @classmethod
    def _validate_readiness_rows(
        cls, value: dict[str, dict[str, int]]
    ) -> dict[str, dict[str, int]]:
        for bank, by_product in value.items():
            unknown = set(by_product) - READINESS_PRODUCT_KEYS
            if unknown:
                raise ValueError(
                    f"readiness_min_rate_rows[{bank!r}] has unknown product(s) {sorted(unknown)}; "
                    f"expected {sorted(READINESS_PRODUCT_KEYS)}"
                )
            if any(rows < 1 for rows in by_product.values()):
                raise ValueError(f"readiness_min_rate_rows[{bank!r}] rows must be >= 1")
        return value

    @property
    def effective_readiness_timeout_seconds(self) -> float:
        if self.browser_readiness_timeout_seconds is None:
            return self.request_timeout_seconds
        return self.browser_readiness_timeout_seconds


def estimated_worst_case_run_seconds(config: CollectionConfig, bank_count: int) -> float:
    """Upper estimate of one browser-rendered product collection that never hangs.

    Every bank exhausts every attempt: browser start-up, a navigation that runs to its
    timeout, then a readiness wait that runs to its timeout; exponential backoff between
    attempts; the politeness delay between banks; plus post-collection alert work. Used to
    document and sanity-check ``collection_run_timeout_seconds`` -- a deadline below this
    would kill runs that are slow but still progressing.
    """
    attempts = config.max_retries + 1
    per_attempt = (
        BROWSER_STARTUP_ALLOWANCE_SECONDS
        + config.request_timeout_seconds
        + config.effective_readiness_timeout_seconds
    )
    backoff = config.retry_backoff_seconds * (2**config.max_retries - 1)
    per_bank = attempts * per_attempt + backoff
    delays = max(bank_count - 1, 0) * config.inter_request_delay_seconds
    return bank_count * per_bank + delays + POST_COLLECTION_ALLOWANCE_SECONDS
