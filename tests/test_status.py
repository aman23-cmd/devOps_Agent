"""Tests for api/status.py — /status and /status/recent endpoints."""

from unittest.mock import patch
from fastapi.testclient import TestClient

from api.webhook_receiver import app

client = TestClient(app)


@patch("api.status.get_analytics_summary")
def test_status_endpoint(mock_analytics):
    """GET /status returns health + analytics summary."""
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

    response = client.get("/status")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "devops-pipeline-agent"
    assert "timestamp" in data
    assert data["analytics"]["total_fixes"] == 42
    assert data["analytics"]["success_rate"] == 0.833
    assert data["analytics"]["by_category"]["flaky_test"] == 18


@patch("api.status.get_recent_records")
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


@patch("api.status.get_analytics_summary")
def test_status_endpoint_handles_db_error(mock_analytics):
    """GET /status should still return 200 even if DB query fails."""
    mock_analytics.side_effect = Exception("DB connection refused")

    response = client.get("/status")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "error" in data["analytics"]
