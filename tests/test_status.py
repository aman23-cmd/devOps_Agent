"""Tests for api/status.py — /status, /status/recent, and /status/dlq endpoints."""

import json
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from devops_agent.api.webhook_receiver import app

client = TestClient(app)


@patch("devops_agent.api.status._get_redis")
@patch("devops_agent.api.status.get_analytics_summary")
def test_status_endpoint(mock_analytics, mock_redis):
    """GET /status returns health + analytics summary + DLQ depth."""
    mock_analytics.return_value = {
        "total_fixes": 42,
        "successes": 35,
        "failures": 5,
        "pending": 2,
        "success_rate": 0.833,
        "avg_duration_seconds": 145.7,
        "auto_applied_count": 20,
        "by_category": {"flaky_test": 18, "network_timeout": 12},
        "by_fix_method": {"rerun_failed_jobs": 25, "pr_created": 17},
    }

    redis_mock = AsyncMock()
    redis_mock.llen = AsyncMock(return_value=3)
    redis_mock.aclose = AsyncMock()
    mock_redis.return_value = redis_mock

    response = client.get("/status")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "devops-pipeline-agent"
    assert data["version"] == "2.0.0"
    assert "timestamp" in data
    assert data["dlq_depth"] == 3
    assert data["analytics"]["total_fixes"] == 42
    assert data["analytics"]["success_rate"] == 0.833
    assert data["analytics"]["by_category"]["flaky_test"] == 18


@patch("devops_agent.api.status.get_recent_records")
def test_status_recent_endpoint(mock_recent):
    """GET /status/recent returns last N records."""
    mock_recent.return_value = [
        {
            "id": 1,
            "run_id": 123,
            "repo": "owner/repo",
            "branch": "main",
            "root_cause_category": "flaky_test",
            "fix_outcome": "success",
            "fix_method": "rerun_failed_jobs",
            "auto_applied": True,
            "duration_seconds": 95.5,
            "formatted_duration": "1m 35s",
        },
    ]

    response = client.get("/status/recent?limit=5")

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["records"][0]["run_id"] == 123
    assert data["records"][0]["fix_method"] == "rerun_failed_jobs"
    assert data["records"][0]["formatted_duration"] == "1m 35s"
    mock_recent.assert_called_once_with(limit=5)


@patch("devops_agent.api.status._get_redis")
@patch("devops_agent.api.status.get_analytics_summary")
def test_status_endpoint_handles_db_error(mock_analytics, mock_redis):
    """GET /status should still return 200 even if DB query fails."""
    mock_analytics.side_effect = Exception("DB connection refused")

    redis_mock = AsyncMock()
    redis_mock.llen = AsyncMock(return_value=0)
    redis_mock.aclose = AsyncMock()
    mock_redis.return_value = redis_mock

    response = client.get("/status")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "error" in data["analytics"]


@patch("devops_agent.api.status._get_redis")
def test_dlq_view_endpoint(mock_redis):
    """GET /status/dlq returns DLQ contents without consuming them."""
    sample_event = {
        "run_id": 999,
        "repo_full_name": "owner/repo",
        "branch": "main",
    }
    redis_mock = AsyncMock()
    redis_mock.lrange = AsyncMock(return_value=[json.dumps(sample_event)])
    redis_mock.llen = AsyncMock(return_value=1)
    redis_mock.aclose = AsyncMock()
    mock_redis.return_value = redis_mock

    response = client.get("/status/dlq")

    assert response.status_code == 200
    data = response.json()
    assert data["total_in_dlq"] == 1
    assert data["showing"] == 1
    assert data["events"][0]["run_id"] == 999


@patch("devops_agent.api.status._get_redis")
def test_dlq_replay_endpoint(mock_redis):
    """POST /status/dlq/replay moves events from DLQ to main queue."""
    sample_event = json.dumps({"run_id": 999})
    redis_mock = AsyncMock()
    redis_mock.rpop = AsyncMock(side_effect=[sample_event, None])
    redis_mock.lpush = AsyncMock()
    redis_mock.llen = AsyncMock(return_value=0)
    redis_mock.aclose = AsyncMock()
    mock_redis.return_value = redis_mock

    response = client.post("/status/dlq/replay?count=2")

    assert response.status_code == 200
    data = response.json()
    assert data["replayed"] == 1
    assert data["remaining_in_dlq"] == 0
    redis_mock.lpush.assert_called_once()
