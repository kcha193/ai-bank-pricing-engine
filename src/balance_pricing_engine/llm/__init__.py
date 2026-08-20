"""Tier 4 LLM-assisted diagnostics layer. Requires the [llm] optional extra."""
from balance_pricing_engine.llm._client import is_sdk_installed
from balance_pricing_engine.llm.anomaly import AnomalyReport, ConfigAnomalyChecker
from balance_pricing_engine.llm.model_validation import (
    ModelValidationReportAgent,
    ValidationReportResult,
)
from balance_pricing_engine.llm.narrative import ScenarioNarrativeAgent
from balance_pricing_engine.llm.scenario_interface import NLScenarioInterface, apply_patches

__all__ = [
    "AnomalyReport",
    "ConfigAnomalyChecker",
    "ModelValidationReportAgent",
    "ValidationReportResult",
    "ScenarioNarrativeAgent",
    "NLScenarioInterface",
    "apply_patches",
    "is_sdk_installed",
]
