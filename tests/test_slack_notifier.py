"""Tests for agents/slack_notifier.py — Block Kit structure and button payloads."""
import json
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime, timezone
from api.models import (
    PipelineFailureEvent, DiagnosisResult, FixProposal,
    RiskLevel, RootCauseCategory,
)


@pytest.mark.asyncio
@patch("agents.slack_notifier.AsyncWebClient")
@patch("agents.slack_notifier.get_settings")
async def test_send_failure_alert(mock_settings_fn, mock_ws_cls):
    s = MagicMock()
    s.SLACK_BOT_TOKEN = "xoxb-test"
    s.SLACK_CHANNEL_ID = "C12345"
    mock_settings_fn.return_value = s

    mock_ws = AsyncMock()
    mock_ws_cls.return_value = mock_ws
    mock_ws.chat_postMessage.return_value = {"ts": "12345.67890"}

    from agents.slack_notifier import SlackNotifier
    notifier = SlackNotifier()

    event = PipelineFailureEvent(
        run_id=123, repo_full_name="owner/repo", branch="main",
        commit_sha="a" * 40, workflow_name="ci.yml",
        failed_at=datetime.now(timezone.utc),
        html_url="https://github.com/owner/repo/actions/runs/123",
    )
    diag = DiagnosisResult(
        failure_step="npm install",
        error_message="npm ERR! code ERESOLVE",
        root_cause_category=RootCauseCategory.DEPENDENCY_ISSUE,
        confidence=0.85, explanation="Dep fail.",
        contributing_factors=["ver"], action_required=None,
    )
    fix = FixProposal(
        description="Run npm install --legacy-peer-deps",
        commands=["npm install --legacy-peer-deps"],
        risk_level=RiskLevel.LOW, success_probability=0.9,
    )

    ts = await notifier.send_failure_alert(None, event, diag, [fix])

    assert ts == "12345.67890"
    mock_ws.chat_postMessage.assert_called_once()
    _, kwargs = mock_ws.chat_postMessage.call_args
    assert kwargs["channel"] == "C12345"
    blocks = kwargs["blocks"]
    assert blocks[0]["type"] == "header"
    assert "Pipeline Failure Detected" in blocks[0]["text"]["text"]
    fields = blocks[1]["fields"]
    assert any("owner/repo" in f["text"] for f in fields)
    actions = [b for b in blocks if b["type"] == "actions"][0]
    elems = actions["elements"]
    assert len(elems) == 3
    assert elems[0]["action_id"] == "apply_fix"
    assert elems[1]["action_id"] == "retry_pipeline"
    assert elems[2]["action_id"] == "view_logs"
