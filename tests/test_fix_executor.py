"""
Tests for agents/fix_executor.py — retry detection, rerun API, and PR creation.
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from api.models import FixProposal, RiskLevel
from agents.fix_executor import execute_fix, _is_retry_fix


def test_is_retry_fix_with_retry_description_and_retry_command():
    """A fix that says 'retry' in description AND has commands=['retry'] should be a retry."""
    p_retry = FixProposal(
        description="Retry the failed job since it is a transient error",
        commands=["retry"],
        risk_level=RiskLevel.LOW,
        success_probability=0.8,
    )
    assert _is_retry_fix(p_retry) is True


def test_is_retry_fix_with_no_commands():
    """A fix with retry keyword in description and NO commands is also a retry."""
    p_retry = FixProposal(
        description="Retry the pipeline",
        commands=[],
        risk_level=RiskLevel.LOW,
        success_probability=0.8,
    )
    assert _is_retry_fix(p_retry) is True


def test_is_retry_fix_with_patches():
    """Even if description says 'rerun', file_patches means it's NOT a retry."""
    p_patch = FixProposal(
        description="Rerun failed tests after fixing file",
        commands=["retry"],
        file_patches={"test.py": "print('ok')"},
        risk_level=RiskLevel.LOW,
        success_probability=0.8,
    )
    assert _is_retry_fix(p_patch) is False


def test_is_retry_fix_with_non_retry_commands():
    """Commands that aren't just retry → not a retry fix."""
    p_cmd = FixProposal(
        description="Run a different script",
        commands=["python script.py"],
        risk_level=RiskLevel.LOW,
        success_probability=0.8,
    )
    assert _is_retry_fix(p_cmd) is False


@pytest.mark.asyncio
@patch("agents.fix_executor.httpx.AsyncClient")
@patch("agents.fix_executor.get_settings")
async def test_execute_retry_fix_success(mock_get_settings, mock_client_class):
    # Mock settings
    mock_settings = MagicMock()
    mock_settings.GITHUB_TOKEN = "test_token"
    mock_get_settings.return_value = mock_settings

    mock_client = AsyncMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client

    # Mock POST to rerun-failed-jobs
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_client.post.return_value = mock_resp

    proposal = FixProposal(
        description="Just retry the build",
        commands=["retry"],
        risk_level=RiskLevel.LOW,
        success_probability=0.7,
    )

    result = await execute_fix(
        proposal, run_id=123, repo="owner/repo", base_branch="main"
    )

    assert result.success is True
    assert result.action_taken == "rerun_failed_jobs"
    assert result.github_url == "https://github.com/owner/repo/actions/runs/123"
    mock_client.post.assert_called_once_with(
        "https://api.github.com/repos/owner/repo/actions/runs/123/rerun-failed-jobs",
        headers={
            "Authorization": "Bearer test_token",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


@pytest.mark.asyncio
@patch("agents.fix_executor.httpx.AsyncClient")
@patch("agents.fix_executor.get_settings")
async def test_execute_patch_fix_success(mock_get_settings, mock_client_class):
    # Mock settings
    mock_settings = MagicMock()
    mock_settings.GITHUB_TOKEN = "test_token"
    mock_get_settings.return_value = mock_settings

    mock_client = AsyncMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client

    # Mock responses for ref, commit, blob, tree, new commit, ref create, PR open
    resp_ref = MagicMock()
    resp_ref.json.return_value = {"object": {"sha": "basesha123"}}
    resp_ref.status_code = 200
    resp_ref.raise_for_status = MagicMock()

    resp_commit = MagicMock()
    resp_commit.json.return_value = {"tree": {"sha": "treesha123"}}
    resp_commit.status_code = 200
    resp_commit.raise_for_status = MagicMock()

    resp_blob = MagicMock()
    resp_blob.json.return_value = {"sha": "blobsha123"}
    resp_blob.status_code = 201
    resp_blob.raise_for_status = MagicMock()

    resp_tree = MagicMock()
    resp_tree.json.return_value = {"sha": "newtreesha123"}
    resp_tree.status_code = 201
    resp_tree.raise_for_status = MagicMock()

    resp_new_commit = MagicMock()
    resp_new_commit.json.return_value = {"sha": "newcommitsha123"}
    resp_new_commit.status_code = 201
    resp_new_commit.raise_for_status = MagicMock()

    resp_ref_create = MagicMock()
    resp_ref_create.status_code = 201
    resp_ref_create.raise_for_status = MagicMock()

    resp_pr = MagicMock()
    resp_pr.json.return_value = {"html_url": "https://github.com/owner/repo/pull/1"}
    resp_pr.status_code = 201

    # Map request paths to responses
    def mock_get(url, **kwargs):
        if "git/ref/heads/main" in url:
            return resp_ref
        elif "git/commits/basesha123" in url:
            return resp_commit
        return MagicMock()

    def mock_post(url, **kwargs):
        if "git/blobs" in url:
            return resp_blob
        elif "git/trees" in url:
            return resp_tree
        elif "git/commits" in url:
            return resp_new_commit
        elif "git/refs" in url:
            return resp_ref_create
        elif "pulls" in url:
            return resp_pr
        return MagicMock()

    mock_client.get.side_effect = mock_get
    mock_client.post.side_effect = mock_post

    proposal = FixProposal(
        description="Fix bug in main.py",
        commands=[],
        file_patches={"main.py": "print('fixed')"},
        risk_level=RiskLevel.MEDIUM,
        success_probability=0.9,
    )

    result = await execute_fix(
        proposal, run_id=123, repo="owner/repo", base_branch="main"
    )

    assert result.success is True
    assert result.action_taken == "pr_created"
    assert result.github_url == "https://github.com/owner/repo/pull/1"
