import json
import logging
from unittest.mock import MagicMock

from balance_pricing_engine.llm.model_validation import ModelValidationReportAgent
from balance_pricing_engine.modelling.agentic import ModelRecord


def _model_record(**overrides):
    defaults = dict(
        model_name="HistGBMResponseModel",
        model_type="HistGBMResponseModel",
        version="1",
        trained_at_utc="2026-08-20T00:00:00+00:00",
        artifact_path="data/outputs/models/histgbm_v1.joblib",
        metrics={"auc": 0.812, "log_loss": 0.421, "brier_score": 0.138},
        calibration_mae=0.021,
        governance_status="pending_review",
        champion_status="challenger",
        config_hash="abc123",
        feature_columns=["offered_rate", "competitor_gap_bps"],
        target_column="accepted",
    )
    defaults.update(overrides)
    return ModelRecord(**defaults)


def test_validation_agent_fallback_returns_structured_summary():
    record = _model_record()
    agent = ModelValidationReportAgent(use_llm=False)
    result = agent.draft_report(record)

    assert result.narrative_text
    assert "HistGBMResponseModel" in result.narrative_text
    assert result.used_llm is False
    assert result.model_used == "fallback"
    assert "[DECISION-SUPPORT: requires human review]" in result.narrative_text


def test_validation_agent_fallback_contains_key_metrics():
    record = _model_record()
    agent = ModelValidationReportAgent(use_llm=False)
    result = agent.draft_report(record)

    assert "0.812" in result.narrative_text
    assert "0.021" in result.narrative_text
    assert "pending_review" in result.narrative_text


def test_validation_agent_with_mocked_llm_returns_llm_response():
    record = _model_record()

    mock_message = MagicMock()
    mock_message.content = [MagicMock(text="This is a mocked validation narrative.")]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_message

    agent = ModelValidationReportAgent(use_llm=True, client=mock_client)
    result = agent.draft_report(record)

    assert result.used_llm is True
    assert "mocked validation narrative" in result.narrative_text
    assert "[DECISION-SUPPORT: requires human review]" in result.narrative_text
    assert result.model_used == "claude-sonnet-4-6"
    mock_client.messages.create.assert_called_once()


def test_validation_agent_appends_marker_when_llm_puts_it_mid_response():
    """The system prompt requires the marker to be the final text. A response that
    contains it earlier, followed by an actionable conclusion, must still get the
    marker appended at the end -- a bare substring check would leave that conclusion
    outside the human-review warning."""
    record = _model_record()

    mock_message = MagicMock()
    mock_message.content = [
        MagicMock(
            text=(
                "[DECISION-SUPPORT: requires human review] "
                "This model is approved for deployment."
            )
        )
    ]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_message

    agent = ModelValidationReportAgent(use_llm=True, client=mock_client)
    result = agent.draft_report(record)

    assert result.narrative_text.rstrip().endswith(
        "[DECISION-SUPPORT: requires human review]"
    )


def test_validation_agent_falls_back_on_api_error():
    record = _model_record()

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = Exception("API unavailable")

    agent = ModelValidationReportAgent(use_llm=True, client=mock_client)
    result = agent.draft_report(record)

    assert result.used_llm is False
    assert result.model_used == "fallback"
    assert result.narrative_text


def test_validation_agent_falls_back_silently_when_sdk_missing(monkeypatch, caplog):
    record = _model_record()

    def raise_import_error():
        raise ImportError("anthropic missing")

    monkeypatch.setattr("balance_pricing_engine.llm._client.build_client", raise_import_error)
    caplog.set_level(logging.WARNING)

    result = ModelValidationReportAgent(use_llm=True).draft_report(record)

    assert result.used_llm is False
    assert result.model_used == "fallback"
    assert caplog.text == ""


def test_validation_agent_warns_when_client_build_fails(monkeypatch, caplog):
    record = _model_record()

    def raise_runtime_error():
        raise RuntimeError("bad client config")

    monkeypatch.setattr("balance_pricing_engine.llm._client.build_client", raise_runtime_error)
    caplog.set_level(logging.WARNING)

    result = ModelValidationReportAgent(use_llm=True).draft_report(record)

    assert result.used_llm is False
    assert result.model_used == "fallback"
    assert "Failed to build LLM client" in caplog.text


def test_validation_result_has_prompt_hash():
    record = _model_record()
    agent = ModelValidationReportAgent(use_llm=False)
    result = agent.draft_report(record)
    assert result.prompt_hash


def test_validation_agent_writes_audit_log(tmp_path):
    record = _model_record()
    agent = ModelValidationReportAgent(use_llm=False, audit_log_path=tmp_path / "audit.jsonl")
    agent.draft_report(record)

    log_lines = (tmp_path / "audit.jsonl").read_text().strip().split("\n")
    assert len(log_lines) == 1
    entry = json.loads(log_lines[0])
    assert entry["used_llm"] is False
    assert "prompt_hash" in entry
    assert "timestamp_utc" in entry
