"""Tests for api/slack_handler.py — Slack interaction endpoint.

Covers:
  • apply_fix action — routes to _handle_apply_fix background task
  • retry_pipeline action — routes to _handle_retry_pipeline background task
  • view_logs action — link button, no server action needed
  • invalid signature — returns 401
  • unknown action — returns 200 {ok: true}
"""

import hmac
import hashlib
import json
import time
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from api.webhook_receiver import app

client = TestClient(app)


# ── Helper ───────────────────────────────────────────────────────


def _slack_sig(body_bytes: bytes, ts: str, secret: str) -> str:
    """Compute a valid Slack request signature."""
    base = f"v0:{ts}:{body_bytes.decode('utf-8')}"
    return (
        "v0="
        + hmac.new(
            secret.encode(),
            base.encode(),
            hashlib.sha256,
        ).hexdigest()
    )


def _build_request(action_id: str, extra_value: dict | None = None):
    """Build a Slack interaction request with the given action_id."""
    value = {"run_id": 123, "repo": "owner/repo", "branch": "main"}
    if extra_value:
        value.update(extra_value)
    payload = {
        "type": "block_actions",
        "user": {"username": "amank"},
        "channel": {"id": "C12345"},
        "message": {"ts": "111.222"},
        "actions": [
            {
                "action_id": action_id,
                "value": json.dumps(value),
            }
        ],
    }
    body = f"payload={json.dumps(payload)}"
    body_bytes = body.encode("utf-8")
    ts = str(int(time.time()))
    sig = _slack_sig(body_bytes, ts, "slack_secret")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": sig,
    }
    return body_bytes, headers


# ═════════════════════════════════════════════════════════════════
#  Test: apply_fix action
# ═════════════════════════════════════════════════════════════════


def test_slack_interaction_apply_fix():
    """Verify that a valid Slack interaction returns 200 {ok: true}."""
    body_bytes, headers = _build_request("apply_fix")

    with patch(
        "api.slack_handler._handle_apply_fix", new_callable=AsyncMock
    ) as mock_handler:
        response = client.post(
            "/slack/interact",
            content=body_bytes,
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    # Background task was scheduled with correct args
    mock_handler.assert_called_once()
    call_args = mock_handler.call_args
    assert call_args[0][0] == 123  # run_id
    assert call_args[0][1] == "owner/repo"  # repo
    assert call_args[0][2] == "main"  # branch
    assert call_args[0][3] == "amank"  # user


# ═════════════════════════════════════════════════════════════════
#  Test: retry_pipeline action
# ═════════════════════════════════════════════════════════════════


def test_slack_interaction_retry_pipeline():
    """Verify retry_pipeline routes to _handle_retry_pipeline."""
    body_bytes, headers = _build_request("retry_pipeline")

    with patch(
        "api.slack_handler._handle_retry_pipeline", new_callable=AsyncMock
    ) as mock_handler:
        response = client.post(
            "/slack/interact",
            content=body_bytes,
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    mock_handler.assert_called_once()
    call_args = mock_handler.call_args
    assert call_args[0][0] == 123  # run_id
    assert call_args[0][1] == "owner/repo"  # repo
    assert call_args[0][3] == "amank"  # user


# ═════════════════════════════════════════════════════════════════
#  Test: view_logs action (link button — no server work needed)
# ═════════════════════════════════════════════════════════════════


def test_slack_interaction_view_logs():
    """view_logs is a link button — server returns ok without any background task."""
    body_bytes, headers = _build_request("view_logs")

    # No background handler should be called for view_logs
    with (
        patch(
            "api.slack_handler._handle_apply_fix", new_callable=AsyncMock
        ) as mock_apply,
        patch(
            "api.slack_handler._handle_retry_pipeline", new_callable=AsyncMock
        ) as mock_retry,
    ):
        response = client.post(
            "/slack/interact",
            content=body_bytes,
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    mock_apply.assert_not_called()
    mock_retry.assert_not_called()


# ═════════════════════════════════════════════════════════════════
#  Test: invalid signature → 401
# ═════════════════════════════════════════════════════════════════


def test_slack_interaction_invalid_signature():
    """Request with bad signature should be rejected with 401."""
    body_bytes, headers = _build_request("apply_fix")
    headers["X-Slack-Signature"] = (
        "v0=0000000000000000000000000000000000000000000000000000000000000000"
    )

    response = client.post(
        "/slack/interact",
        content=body_bytes,
        headers=headers,
    )

    assert response.status_code == 401
    assert "Invalid Slack signature" in response.json()["detail"]


# ═════════════════════════════════════════════════════════════════
#  Test: unknown action_id → 200 ok (no crash)
# ═════════════════════════════════════════════════════════════════


def test_slack_interaction_unknown_action():
    """Unknown action_id should gracefully return ok without crashing."""
    body_bytes, headers = _build_request("some_unknown_action")

    response = client.post(
        "/slack/interact",
        content=body_bytes,
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
