import logging
import json
from unittest.mock import MagicMock

from balance_pricing_engine.config import load_config
from balance_pricing_engine.llm.narrative import ScenarioNarrativeAgent
from balance_pricing_engine.scenarios.runner import run_baseline_diagnostics


def _diagnostics():
    return run_baseline_diagnostics(load_config("configs/base.yaml"))


def test_narrative_agent_fallback_returns_structured_summary():
    diag = _diagnostics()
    agent = ScenarioNarrativeAgent(use_llm=False)
    result = agent.narrate(diag)

    assert result.narrative_text
    assert "8." in result.narrative_text or "loan" in result.narrative_text.lower()
    assert result.used_llm is False
    assert result.model_used == "fallback"
    assert "[DECISION-SUPPORT: requires human review]" in result.narrative_text


def test_narrative_agent_fallback_contains_key_numbers():
    diag = _diagnostics()
    agent = ScenarioNarrativeAgent(use_llm=False)
    result = agent.narrate(diag)
    # Should mention the best loan rate as a percentage
    assert f"{diag.best_result.loan_rate:.2%}" in result.narrative_text or (
        f"{diag.best_result.loan_rate * 100:.2f}" in result.narrative_text
    )


def test_narrative_agent_with_mocked_llm_returns_llm_response():
    diag = _diagnostics()

    mock_message = MagicMock()
    mock_message.content = [MagicMock(text="This is a mocked LLM narrative about pricing.")]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_message

    agent = ScenarioNarrativeAgent(use_llm=True, client=mock_client)
    result = agent.narrate(diag)

    assert result.used_llm is True
    assert "mocked LLM narrative" in result.narrative_text
    assert "[DECISION-SUPPORT: requires human review]" in result.narrative_text
    assert result.model_used == "claude-sonnet-4-6"
    mock_client.messages.create.assert_called_once()


def test_narrative_agent_falls_back_on_api_error():
    diag = _diagnostics()

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = Exception("API unavailable")

    agent = ScenarioNarrativeAgent(use_llm=True, client=mock_client)
    result = agent.narrate(diag)

    assert result.used_llm is False
    assert result.model_used == "fallback"
    assert result.narrative_text  # fallback text still present


def test_narrative_agent_falls_back_silently_when_sdk_missing(monkeypatch, caplog):
    diag = _diagnostics()

    def raise_import_error():
        raise ImportError("anthropic missing")

    monkeypatch.setattr("balance_pricing_engine.llm._client.build_client", raise_import_error)
    caplog.set_level(logging.WARNING)

    result = ScenarioNarrativeAgent(use_llm=True).narrate(diag)

    assert result.used_llm is False
    assert result.model_used == "fallback"
    assert caplog.text == ""


def test_narrative_agent_warns_when_client_build_fails(monkeypatch, caplog):
    diag = _diagnostics()

    def raise_runtime_error():
        raise RuntimeError("bad client config")

    monkeypatch.setattr("balance_pricing_engine.llm._client.build_client", raise_runtime_error)
    caplog.set_level(logging.WARNING)

    result = ScenarioNarrativeAgent(use_llm=True).narrate(diag)

    assert result.used_llm is False
    assert result.model_used == "fallback"
    assert "Failed to build LLM client" in caplog.text


def test_narrative_result_has_prompt_hash():
    diag = _diagnostics()
    agent = ScenarioNarrativeAgent(use_llm=False)
    result = agent.narrate(diag)
    assert result.prompt_hash  # non-empty string


def test_narrative_agent_writes_audit_log(tmp_path):
    diag = _diagnostics()
    agent = ScenarioNarrativeAgent(use_llm=False, audit_log_path=tmp_path / "audit.jsonl")
    agent.narrate(diag)

    log_lines = (tmp_path / "audit.jsonl").read_text().strip().split("\n")
    assert len(log_lines) == 1
    entry = json.loads(log_lines[0])
    assert entry["used_llm"] is False
    assert "prompt_hash" in entry
    assert "timestamp_utc" in entry
