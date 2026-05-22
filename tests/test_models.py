from datetime import datetime, timezone
import pytest
from pydantic import ValidationError
from api.models import (
    PipelineFailureEvent,
    DiagnosisResult,
    FixProposal,
    RiskLevel,
    RootCauseCategory,
)


def test_pipeline_failure_event_valid():
    event = PipelineFailureEvent(
        run_id=12345,
        repo_full_name="owner/repo",
        branch="main",
        commit_sha="a" * 40,
        workflow_name="ci.yml",
        failed_at=datetime.now(timezone.utc),
        html_url="https://github.com/owner/repo/actions/runs/12345",
    )
    assert event.run_id == 12345
    assert event.repo_full_name == "owner/repo"
    assert event.commit_sha == "a" * 40


def test_pipeline_failure_event_missing_field():
    with pytest.raises(ValidationError):
        # Missing run_id and repo_full_name
        PipelineFailureEvent(branch="main", commit_sha="a" * 40, workflow_name="ci.yml")


def test_diagnosis_result_valid():
    diag = DiagnosisResult(
        failure_step="npm test",
        error_message="AssertionError: expected false to be true",
        root_cause_category=RootCauseCategory.FLAKY_TEST,
        confidence=0.9,
        explanation="The test is flaky.",
        contributing_factors=["Race condition"],
        action_required=None,
    )
    assert diag.root_cause_category == RootCauseCategory.FLAKY_TEST
    assert diag.confidence == 0.9


def test_fix_proposal_valid():
    proposal = FixProposal(
        description="Rerun failed jobs",
        commands=["retry"],
        file_patches=None,
        risk_level=RiskLevel.LOW,
        success_probability=0.85,
    )
    assert proposal.risk_level == RiskLevel.LOW
    assert proposal.success_probability == 0.85
