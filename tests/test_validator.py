"""Tests for agents/validator.py — polling, success/failure, PagerDuty."""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from agents.validator import _format_duration


def test_format_duration():
    assert _format_duration(45) == "45s"
    assert _format_duration(125) == "2m 5s"
    assert _format_duration(3665) == "1h 1m"


@pytest.mark.asyncio
@patch("agents.validator.get_settings")
@patch("agents.validator.SlackNotifier")
@patch("agents.validator.httpx.AsyncClient")
@patch("agents.validator.get_session")
async def test_validate_fix_success(
    mock_get_session, mock_http_cls, mock_slack_cls, mock_settings_fn
):
    # Mock settings
    s = MagicMock()
    s.GITHUB_TOKEN = "tok"
    s.SLACK_BOT_TOKEN = "xoxb"
    s.SLACK_CHANNEL_ID = "C1"
    s.PAGERDUTY_ROUTING_KEY = None
    mock_settings_fn.return_value = s

    mock_http = AsyncMock()
    mock_http_cls.return_value.__aenter__.return_value = mock_http

    mock_slack = MagicMock()
    mock_slack.send_thread_update = AsyncMock()
    mock_slack.send_resolution = AsyncMock()
    mock_slack_cls.return_value = mock_slack

    mock_db = MagicMock()
    mock_get_session.return_value = mock_db
    mock_rec = MagicMock()
    mock_rec.fix_outcome = "pending"
    mock_db.query.return_value.filter_by.return_value.order_by.return_value.first.return_value = mock_rec

    resp_info = MagicMock()
    resp_info.json.return_value = {"created_at": "2026-05-20T12:00:00Z"}
    resp_info.status_code = 200
    resp_info.raise_for_status = MagicMock()

    resp_list = MagicMock()
    resp_list.json.return_value = {
        "workflow_runs": [
            {
                "id": 100001,
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-05-20T12:05:00Z",
            }
        ]
    }
    resp_list.status_code = 200
    resp_list.raise_for_status = MagicMock()

    def mock_get(url, **kw):
        return resp_info if "runs/123" in url else resp_list

    mock_http.get.side_effect = mock_get

    from agents.fix_executor import ExecutionResult
    from agents.validator import FixValidator

    validator = FixValidator()
    er = ExecutionResult(
        success=True,
        action_taken="rerun_failed_jobs",
        github_url="https://github.com/owner/repo/pull/1",
    )

    with patch("agents.validator.POLL_INTERVAL_SECONDS", 0.01):
        result = await validator.validate_fix(
            repo="owner/repo",
            branch="main",
            run_id=123,
            execution_result=er,
            slack_ts="111.222",
        )

    assert result["validated"] is True
    assert result["new_run_id"] == 100001
    assert result["new_conclusion"] == "success"
    mock_slack.send_resolution.assert_called_once()
