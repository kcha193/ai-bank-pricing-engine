"""
Tier 4 LLM diagnostics example.

Runs config anomaly detection (rule-based, no API key needed) and
optional scenario narration (requires anthropic SDK + ANTHROPIC_API_KEY).

Usage:
    # Anomaly detection only (no API key needed):
    python3 examples/run_llm_diagnostics.py

    # With LLM narration (requires: pip install 'balance_pricing_engine[llm]'):
    ANTHROPIC_API_KEY=sk-ant-... python3 examples/run_llm_diagnostics.py --narrate
"""
from __future__ import annotations

import argparse

from balance_pricing_engine.config import load_config
from balance_pricing_engine.llm import AnomalyReport, ConfigAnomalyChecker, ScenarioNarrativeAgent
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics


def main() -> None:
    parser = argparse.ArgumentParser(description="Tier 4 LLM diagnostics")
    parser.add_argument("--config", default="configs/base.yaml", help="Config file path")
    parser.add_argument(
        "--narrate",
        action="store_true",
        help="Generate scenario narration via Claude API (requires [llm] extra + API key)",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    print("=== Config Anomaly Check ===")
    checker = ConfigAnomalyChecker()
    reports: list[AnomalyReport] = checker.check(config)
    if not reports:
        print("  No anomalies detected.")
    else:
        for report in reports:
            print(f"  [{report.severity.upper()}] {report.rule_id}: {report.message}")
    print()

    if args.narrate:
        print("=== Scenario Narration (LLM) ===")
        diagnostics = run_baseline_diagnostics(config)
        from balance_pricing_engine.llm._client import is_llm_available

        use_llm = is_llm_available()
        if not use_llm:
            print("  anthropic SDK not installed or API key not configured - using deterministic fallback.")
        agent = ScenarioNarrativeAgent(use_llm=use_llm)
        result = agent.narrate(diagnostics)
        print(f"  Model used: {result.model_used}")
        print(f"  Prompt hash: {result.prompt_hash}")
        print()
        print(result.narrative_text)
    else:
        print("Tip: run with --narrate to generate a scenario narrative (requires [llm] extra).")


if __name__ == "__main__":
    main()
