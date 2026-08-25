"""HTTP behaviour configuration for public bank page collection.

Lives in ``competitor_rates``, not ``pricing_intelligence``: collectors are the
lower-level ingestion package ``pricing_intelligence`` is built on top of (see
AGENTS.md), so this config must not be defined in a package the collectors
would then have to import back up to.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

DEFAULT_USER_AGENT = (
    "ai-bank-pricing-engine/0.1 (+pricing-intelligence; public advertised rates only)"
)


class CollectionConfig(BaseModel):
    """HTTP behaviour for public bank page collection."""

    request_timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    retry_backoff_seconds: float = Field(default=2.0, ge=0)
    # Politeness delay between requests to the same host. Public pages only; this
    # keeps collection well below anything a bank would consider aggressive.
    inter_request_delay_seconds: float = Field(default=1.0, ge=0)
    user_agent: str = DEFAULT_USER_AGENT
