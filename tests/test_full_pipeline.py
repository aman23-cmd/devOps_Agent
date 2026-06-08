import json
import hmac
import hashlib
import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, MagicMock, AsyncMock

from devops_agent.api.webhook_receiver import app
from devops_agent.config.settings import get_settings
from devops_agent.api.models import (
    PipelineFailureEvent,
    DiagnosisResult,
    FixProposal,
    RiskLevel,
    RootCauseCategory,
)
from devops_agent.agents.worker import AgentWorker


@pytest.fixture
def mock_settings():
    settings = get_settings()
    # Ensure test secrets
    settings.GITHUB_WEBHOOK_SECRET = "test_secret"
    return settings


def create_github_signature(payload: bytes, secret: str) -> str:
    digest = hmac.new(key=secret.encode("utf-8"), msg=payload, digestmod=hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.mark.asyncio
@patch("devops_agent.api.webhook_receiver._get_redis", new_callable=AsyncMock)
async def test_webhook_to_redis_enqueue(mock_get_redis, mock_settings):
    """Test webhook parses payload and enqueues to Redis."""
    mock_redis = AsyncMock()
    mock_get_redis.return_value = mock_redis
    # Configure Redis mock return values
    mock_redis.lpush.return_value = 1
    mock_redis.llen.return_value = 1

    payload = {
        "action": "completed",
        "workflow_run": {
            "id": 999,
            "conclusion": "failure",
            "name": "CI Pipeline",
            "head_branch": "main",
            "head_sha": "abc123def456",
            "updated_at": "2026-05-25T10:00:00Z",
            "html_url": "https://github.com/org/repo/runs/999",
        },
        "repository": {"full_name": "org/repo"},
    }
    payload_bytes = json.dumps(payload).encode("utf-8")
    signature = create_github_signature(payload_bytes, mock_settings.GITHUB_WEBHOOK_SECRET)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhook/github",
            content=payload_bytes,
            headers={
                "X-Hub-Signature-256": signature,
                "X-GitHub-Event": "workflow_run",
            },
        )

    assert response.status_code == 200
    assert response.json()["action"] == "enqueued"
    assert response.json()["run_id"] == 999

    # Verify Redis enqueue
    mock_redis.lpush.assert_called_once()
    call_args = mock_redis.lpush.call_args[0]
    assert call_args[0] == mock_settings.REDIS_QUEUE_KEY
    enqueued_event = json.loads(call_args[1])
    assert enqueued_event["run_id"] == 999
    assert enqueued_event["repo_full_name"] == "org/repo"


@pytest.mark.asyncio
@patch("devops_agent.agents.worker.run_diagnosis_workflow")
@patch("devops_agent.agents.worker.generate_fix_proposals")
@patch("devops_agent.agents.worker.SlackNotifier")
@patch("devops_agent.agents.worker.get_session")
async def test_worker_process_event_integration(
    mock_get_session, mock_slack_class, mock_gen_fixes, mock_diagnosis
):
    """Test the worker full flow: diagnosis -> fix gen -> slack -> db."""
    event = PipelineFailureEvent(
        run_id=888,
        repo_full_name="org/integration-test-repo",
        branch="main",
        commit_sha="abcd123",
        workflow_name="CI Pipeline",
        html_url="https://github.com/org/integration-test-repo/actions/runs/888",
    )

    # Mock Diagnosis output
    mock_diagnosis.return_value = {
        "diagnosis": DiagnosisResult(
            failure_step="npm test",
            error_message="ReferenceError: foo is not defined",
            root_cause_category=RootCauseCategory.CODE_REGRESSION,
            confidence=0.9,
            explanation="Test failed because foo is missing.",
            contributing_factors=[],
            action_required=None,
        ).model_dump()
    }

    # Mock Fix Proposals
    mock_gen_fixes.return_value = [
        FixProposal(
            description="Fix foo reference",
            commands=["echo 'fixed'"],
            file_patches={"app.js": "const foo = 1;"},
            risk_level=RiskLevel.LOW,
            success_probability=0.95,
        )
    ]

    # Use AsyncMock for the notifier since send_failure_alert is async
    mock_notifier_instance = AsyncMock()
    mock_notifier_instance.send_failure_alert.return_value = "12345.67890"
    mock_slack_class.return_value = mock_notifier_instance

    # Mock the DB session to avoid needing a real PostgreSQL connection
    mock_session = AsyncMock()
    # Mock the async context manager behavior
    mock_session.__aenter__.return_value = mock_session
    mock_get_session.return_value = MagicMock(return_value=mock_session)

    worker = AgentWorker()
    worker._notifier = mock_notifier_instance

    await worker._process_event(event)

    # 1. Assert Slack send_failure_alert was called
    mock_notifier_instance.send_failure_alert.assert_called_once()
    args, kwargs = mock_notifier_instance.send_failure_alert.call_args
    assert kwargs["event"] == event
    assert kwargs["diagnosis"].root_cause_category == RootCauseCategory.CODE_REGRESSION
    assert len(kwargs["fix_proposals"]) == 1

    # 2. Assert fix_history DB record was written via session
    mock_session.add.assert_called_once()
    record = mock_session.add.call_args[0][0]

    assert record.run_id == 888
    assert record.repo == "org/integration-test-repo"
    assert record.root_cause_category == "code_regression"
    assert record.confidence == 0.9
    assert record.slack_thread_ts == "12345.67890"

    mock_session.commit.assert_called_once()
    mock_session.close.assert_called_once()
