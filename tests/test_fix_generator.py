"""
Tests for agents/fix_generator.py — keyword extraction, auto-fix policy,
and LLM proposal generation.
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from api.models import DiagnosisResult, FixProposal, PipelineFailureEvent, RiskLevel, RootCauseCategory
from agents.fix_generator import should_auto_apply, _extract_keywords, generate_fix_proposals


def test_extract_keywords():
    error_msg = "Connection timeout to database server. Unable to connect."
    keywords = _extract_keywords(error_msg)
    # _extract_keywords strips punctuation, filters words > 3 chars, and removes noise words.
    # Noise includes: "error", "to", "unable", etc.
    # "connection" (10 chars, not noise) → included
    # "timeout" (7 chars, not noise) → included
    # "database" (8 chars, not noise) → included
    # "server." → stripped to "server" (6 chars, not noise) → included
    assert "connection" in keywords
    assert "timeout" in keywords
    assert "database" in keywords
    assert "server" in keywords
    # Noise words should be excluded
    assert "unable" not in keywords


def test_extract_keywords_filters_short_words():
    error_msg = "npm ERR code ERESOLVE"
    keywords = _extract_keywords(error_msg)
    # "npm" → 3 chars, filtered (> 3 required, i.e. >= 4)
    # "ERR" → 3 chars, filtered
    # "code" → 4 chars, not in noise → included
    # "ERESOLVE" → 8 chars → included
    assert "code" in keywords
    assert "eresolve" in keywords


def test_should_auto_apply_rules():
    # 1. Matches whitelist + low risk + high confidence + high probability
    diag_success = DiagnosisResult(
        failure_step="run tests",
        error_message="flaky test failure",
        root_cause_category=RootCauseCategory.FLAKY_TEST,
        confidence=0.85,
        explanation="Test is flaky",
        contributing_factors=[]
    )
    fix_success = FixProposal(
        description="Retry the failed jobs",
        commands=["retry"],
        risk_level=RiskLevel.LOW,
        success_probability=0.75
    )
    assert should_auto_apply(diag_success, fix_success) is True

    # 2. Risk is MEDIUM -> should NOT auto apply
    fix_medium_risk = FixProposal(
        description="Modify test parameters",
        commands=["modify"],
        risk_level=RiskLevel.MEDIUM,
        success_probability=0.80
    )
    assert should_auto_apply(diag_success, fix_medium_risk) is False

    # 3. Confidence is low (< 0.80) -> should NOT auto apply
    diag_low_confidence = DiagnosisResult(
        failure_step="run tests",
        error_message="flaky test failure",
        root_cause_category=RootCauseCategory.FLAKY_TEST,
        confidence=0.75,
        explanation="Test is flaky",
        contributing_factors=[]
    )
    assert should_auto_apply(diag_low_confidence, fix_success) is False

    # 4. Not in whitelist -> should NOT auto apply
    diag_not_whitelisted = DiagnosisResult(
        failure_step="npm install",
        error_message="dependency mismatch",
        root_cause_category=RootCauseCategory.DEPENDENCY_ISSUE,
        confidence=0.90,
        explanation="Dependency is missing",
        contributing_factors=[]
    )
    assert should_auto_apply(diag_not_whitelisted, fix_success) is False


@pytest.mark.asyncio
@patch("agents.fix_generator.anthropic.AsyncAnthropic")
@patch("agents.fix_generator.query_fix_history")
@patch("agents.fix_generator.get_settings")
async def test_generate_fix_proposals(mock_get_settings, mock_query_history, mock_anthropic_class):
    # Mock settings
    mock_settings = MagicMock()
    mock_settings.ANTHROPIC_API_KEY = "test_key"
    mock_settings.ANTHROPIC_MODEL = "claude-sonnet"
    mock_get_settings.return_value = mock_settings

    # Setup history mock
    mock_query_history.return_value = [
        {
            "fix_applied": "Retry",
            "fix_commands": ["retry"],
            "fix_outcome": "success",
            "confidence": 0.9,
            "risk_level": "LOW",
            "repo": "test/repo",
            "error_message": "flaky test",
            "root_cause_category": "flaky_test",
            "created_at": "2026-05-20"
        }
    ]

    # Setup anthropic client mock
    mock_client = AsyncMock()
    mock_anthropic_class.return_value = mock_client

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text='[{"description": "Retry pipeline", "commands": ["retry"], "file_patches": null, "risk_level": "LOW", "success_probability": 0.95}]')]
    mock_client.messages.create.return_value = mock_response

    event = PipelineFailureEvent(
        run_id=123,
        repo_full_name="test/repo",
        branch="main",
        commit_sha="sha",
        workflow_name="ci.yml"
    )
    diag = DiagnosisResult(
        failure_step="step",
        error_message="err",
        root_cause_category=RootCauseCategory.FLAKY_TEST,
        confidence=0.8,
        explanation="expl"
    )

    proposals = await generate_fix_proposals(event, diag)

    assert len(proposals) == 1
    assert proposals[0].description == "Retry pipeline"
    assert proposals[0].risk_level == RiskLevel.LOW
    assert proposals[0].success_probability == 0.95
