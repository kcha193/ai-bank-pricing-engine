"""Dry-run worker: one non-persisting operational cycle, result written as JSON.

Run as a subprocess by ``actions.run_dry`` so live page rendering (Playwright) stays out of
Streamlit's process. Persists nothing: the store is opened read-only (a throwaway copy, or
in-memory when the database does not exist) and alerts go to the console, never to Telegram or
a webhook.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional, Sequence

from balance_pricing_engine.competitor_rates.models import ProductType
from balance_pricing_engine.dashboard.settings import resolve_intelligence_config
from balance_pricing_engine.pricing_intelligence.orchestration import (
    build_summary,
    run_operational_cycle,
)
from balance_pricing_engine.pricing_intelligence.service import PricingIntelligenceService
from balance_pricing_engine.pricing_intelligence.store import ObservationStore


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--config", required=True, help="EngineConfig YAML (may not exist)")
    parser.add_argument("--database", required=True, help="Observation store path")
    parser.add_argument("--result-file", required=True, help="Where to write the JSON result")
    parser.add_argument("--render-javascript", action="store_true")
    parser.add_argument(
        "--product",
        default=ProductType.HOME_LOAN.value,
        choices=[product.value for product in ProductType],
        help="Which market to dry-run (default: home_loan, as before this option existed)",
    )
    args = parser.parse_args(argv)
    product = ProductType(args.product)

    config_path = Path(args.config)
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    config = resolve_intelligence_config(text).model_copy(update={"database_path": args.database})
    with PricingIntelligenceService(
        config=config,
        store=ObservationStore(config.database_path, read_only=True),
        render_javascript=args.render_javascript,
    ) as service:
        result = run_operational_cycle(service, product_type=product, dry_run=True)
    Path(args.result_file).write_text(
        json.dumps(
            {
                "status": result.status,
                "summary": build_summary(result.outcome, product),
            }
        ),
        encoding="utf-8",
    )
    return result.code


if __name__ == "__main__":
    raise SystemExit(main())
