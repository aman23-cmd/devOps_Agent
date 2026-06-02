import pytest
from unittest.mock import patch, MagicMock
from api.models import RootCauseCategory
from agents.coordinator import run_diagnosis_workflow
from api.models import PipelineFailureEvent
from datetime import datetime, timezone
import json

from tests.mock_logs import generate_mock_log


def get_dummy_event() -> PipelineFailureEvent:
    return PipelineFailureEvent(
        run_id=123,
        repo_full_name="org/repo",
        branch="main",
        commit_sha="abcdef123456",
        workflow_name="CI",
        failed_at=datetime.now(timezone.utc),
        html_url="https://github.com/org/repo/actions/runs/123",
    )


def create_mock_chat_messages(category: str, confidence: float = 0.9) -> list[dict]:
    """Generates mock chat messages simulating AutoGen agent responses."""
    log_content = generate_mock_log(category)

    # Simulate the JSON output from DiagnosisAgent
    mock_diagnosis = {
        "diagnosis": {
            "failure_step": "mocked_step",
            "error_message": "mocked error",
            "root_cause_category": category,
            "confidence": confidence,
            "explanation": "This is a mocked explanation.",
            "contributing_factors": ["factor 1"],
            "action_required": "human_review" if confidence < 0.8 else None,
        },
        "fix_proposals": [],
    }

    return [
        {"name": "Coordinator", "content": "Fetch logs"},
        {"name": "LogFetcher", "content": log_content},
        {
            "name": "DiagnosisAgent",
            "content": f"```json\n{json.dumps(mock_diagnosis)}\n```",
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "category, expected_enum",
    [
        ("flaky_test", RootCauseCategory.FLAKY_TEST),
        ("dependency_issue", RootCauseCategory.DEPENDENCY_ISSUE),
        ("env_mismatch", RootCauseCategory.ENV_MISMATCH),
        ("resource_exhaustion", RootCauseCategory.RESOURCE_EXHAUSTION),
        ("network_timeout", RootCauseCategory.NETWORK_TIMEOUT),
        ("code_regression", RootCauseCategory.CODE_REGRESSION),
        ("config_error", RootCauseCategory.CONFIG_ERROR),
        ("infrastructure_failure", RootCauseCategory.INFRASTRUCTURE_FAILURE),
        ("unknown", RootCauseCategory.UNKNOWN),
        ("FLAKY_TEST", RootCauseCategory.FLAKY_TEST),  # Test uppercase handling
    ],
)
@patch("agents.coordinator._create_group_chat")
@patch("agents.coordinator._create_agents")
async def test_diagnosis_categories(
    mock_create_agents, mock_create_chat, category, expected_enum
):
    """Test DiagnosisAgent parses all 8 failure categories correctly with 10 variations."""
    event = get_dummy_event()

    # Mock agents
    mock_coordinator = MagicMock()
    mock_create_agents.return_value = (
        mock_coordinator,
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )

    # Mock GroupChat to return our pre-canned messages
    mock_group_chat = MagicMock()
    mock_group_chat.messages = create_mock_chat_messages(category)
    mock_create_chat.return_value = (mock_group_chat, MagicMock())

    # Run workflow
    result = await run_diagnosis_workflow(event)

    # Verify outputs
    diag = result["diagnosis"]
    assert diag["root_cause_category"] == expected_enum.value
    assert 0.0 <= diag["confidence"] <= 1.0
    assert "explanation" in diag


@pytest.mark.asyncio
@patch("agents.coordinator._create_group_chat")
@patch("agents.coordinator._create_agents")
async def test_diagnosis_confidence_triggers_human_review(
    mock_create_agents, mock_create_chat
):
    """Test that confidence < 0.80 automatically triggers human_review."""
    event = get_dummy_event()

    mock_coordinator = MagicMock()
    mock_create_agents.return_value = (
        mock_coordinator,
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )

    mock_group_chat = MagicMock()
    # Provide a confidence < 0.80
    mock_group_chat.messages = create_mock_chat_messages("unknown", confidence=0.75)
    mock_create_chat.return_value = (mock_group_chat, MagicMock())

    result = await run_diagnosis_workflow(event)

    diag = result["diagnosis"]
    assert diag["confidence"] == 0.75
    assert diag["action_required"] == "human_review"
    assert result["action_taken"] == "escalated_to_human"


@pytest.mark.asyncio
@patch("agents.coordinator._create_group_chat")
@patch("agents.coordinator._create_agents")
async def test_diagnosis_high_confidence_no_human_review(
    mock_create_agents, mock_create_chat
):
    """Test that confidence >= 0.80 does not trigger human_review."""
    event = get_dummy_event()

    mock_coordinator = MagicMock()
    mock_create_agents.return_value = (
        mock_coordinator,
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )

    mock_group_chat = MagicMock()
    # Provide a confidence >= 0.80
    mock_group_chat.messages = create_mock_chat_messages("flaky_test", confidence=0.95)
    mock_create_chat.return_value = (mock_group_chat, MagicMock())

    result = await run_diagnosis_workflow(event)

    diag = result["diagnosis"]
    assert diag["confidence"] == 0.95
    assert diag["action_required"] is None
