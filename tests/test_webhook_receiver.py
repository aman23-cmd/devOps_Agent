"""
Tests for api/webhook_receiver.py — signature verification, event filtering,
and Redis enqueuing.
"""
import hmac
import hashlib
import json
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

# conftest.py sets env vars at import time, so Settings won't blow up
from api.webhook_receiver import app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "service": "webhook-receiver"}


def test_webhook_invalid_signature():
    response = client.post(
        "/webhook/github",
        json={"dummy": "data"},
        headers={
            "X-Hub-Signature-256": "sha256=invalid_signature",
            "X-GitHub-Event": "workflow_run"
        }
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid signature"}


@patch("api.webhook_receiver._get_redis")
def test_webhook_valid_signature_ignored_event(mock_get_redis):
    secret = "test_secret"
    payload = {"action": "completed", "workflow_run": {"conclusion": "success"}}
    payload_bytes = json.dumps(payload).encode("utf-8")
    signature = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()

    response = client.post(
        "/webhook/github",
        content=payload_bytes,
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "workflow_run",
            "Content-Type": "application/json"
        }
    )
    assert response.status_code == 200
    assert response.json()["action"] == "ignored"


@patch("api.webhook_receiver._get_redis")
def test_webhook_valid_signature_failed_event(mock_get_redis):
    # Setup mock Redis
    mock_redis = AsyncMock()
    mock_redis.llen.return_value = 1
    mock_get_redis.return_value = mock_redis

    secret = "test_secret"
    payload = {
        "action": "completed",
        "workflow_run": {
            "id": 99999,
            "conclusion": "failure",
            "head_branch": "feature-test",
            "head_sha": "abc123sha",
            "name": "CI Build",
            "updated_at": "2026-05-20T12:00:00Z",
            "html_url": "https://github.com/test/repo/actions/runs/99999"
        },
        "repository": {
            "full_name": "test/repo"
        }
    }
    payload_bytes = json.dumps(payload).encode("utf-8")
    signature = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()

    response = client.post(
        "/webhook/github",
        content=payload_bytes,
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "workflow_run",
            "Content-Type": "application/json"
        }
    )

    assert response.status_code == 200
    assert response.json()["action"] == "enqueued"
    assert response.json()["run_id"] == 99999

    # Check that lpush was called with the correct data
    mock_redis.lpush.assert_called_once()
    called_args = mock_redis.lpush.call_args[0]
    assert called_args[0] == "pipeline_failures"
    enqueued_event = json.loads(called_args[1])
    assert enqueued_event["run_id"] == 99999
    assert enqueued_event["repo_full_name"] == "test/repo"
    assert enqueued_event["branch"] == "feature-test"
