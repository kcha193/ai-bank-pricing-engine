"""Tier 4 LLM-assisted diagnostics layer. Requires the [llm] optional extra."""
from balance_pricing_engine.llm.anomaly import AnomalyReport, ConfigAnomalyChecker
from balance_pricing_engine.llm.narrative import ScenarioNarrativeAgent

__all__ = ["AnomalyReport", "ConfigAnomalyChecker", "ScenarioNarrativeAgent"]
