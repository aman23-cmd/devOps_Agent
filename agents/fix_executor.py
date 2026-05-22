"""
Fix Executor — Production-safe fix application via GitHub API only.

CRITICAL SAFETY RULE: This module NEVER executes arbitrary shell commands
in production. ALL mutations go through the GitHub REST API:
  1. "retry"       → POST /repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs
  2. "pr_with_patch" → Create branch via Git Trees API → commit → open PR

This is the only module that writes to GitHub.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from api.models import FixProposal
from config.settings import get_settings

logger = logging.getLogger("fix_executor")

GITHUB_API = "https://api.github.com"


@dataclass
class ExecutionResult:
    """Outcome of a fix execution attempt."""

    success: bool
    action_taken: str  # "rerun_failed_jobs" | "pr_created" | "none"
    github_url: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _headers(token: str) -> dict[str, str]:
    """Standard GitHub API request headers."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# ═════════════════════════════════════════════════════════════════
#  Public API
# ═════════════════════════════════════════════════════════════════


async def execute_fix(
    fix_proposal: FixProposal,
    run_id: int,
    repo: str,
    base_branch: str = "main",
) -> ExecutionResult:
    """
    Execute a fix proposal using ONLY GitHub API calls.

    Routes to the appropriate strategy based on fix content:
      - If fix has file_patches → create a PR with those changes
      - If fix commands contain only "retry" keywords → rerun failed jobs
      - Otherwise → create a PR with commands documented in the body

    Args:
        fix_proposal: The FixProposal from the diagnosis/generator
        run_id: GitHub Actions run ID that failed
        repo: Full repo name (owner/repo)
        base_branch: Branch to target for PRs

    Returns:
        ExecutionResult with action taken and GitHub URL
    """
    settings = get_settings()
    token = settings.GITHUB_TOKEN

    # Determine fix strategy
    is_retry = _is_retry_fix(fix_proposal)

    try:
        if is_retry:
            return await _rerun_failed_jobs(repo, run_id, token)

        elif fix_proposal.file_patches:
            return await _create_pr_with_patches(
                repo=repo,
                run_id=run_id,
                base_branch=base_branch,
                fix=fix_proposal,
                token=token,
            )

        else:
            # Commands but no patches — document commands in a PR
            return await _create_pr_with_commands(
                repo=repo,
                run_id=run_id,
                base_branch=base_branch,
                fix=fix_proposal,
                token=token,
            )

    except Exception as exc:
        logger.error("Fix execution failed: %s", exc, exc_info=True)
        return ExecutionResult(
            success=False,
            action_taken="none",
            error=str(exc),
        )


# ═════════════════════════════════════════════════════════════════
#  Strategy: Rerun Failed Jobs
# ═════════════════════════════════════════════════════════════════


async def _rerun_failed_jobs(
    repo: str,
    run_id: int,
    token: str,
) -> ExecutionResult:
    """
    POST /repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs

    This is the safest fix — just re-triggers the failed jobs.
    Useful for flaky tests, transient network issues, etc.
    """
    url = f"{GITHUB_API}/repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs"

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, headers=_headers(token))

        if response.status_code == 201:
            logger.info("Rerun triggered — run_id=%d repo=%s", run_id, repo)
            return ExecutionResult(
                success=True,
                action_taken="rerun_failed_jobs",
                github_url=f"https://github.com/{repo}/actions/runs/{run_id}",
                details={"rerun_run_id": run_id},
            )
        else:
            error_msg = f"GitHub API {response.status_code}: {response.text[:300]}"
            logger.error("Rerun failed: %s", error_msg)
            return ExecutionResult(
                success=False,
                action_taken="rerun_failed_jobs",
                error=error_msg,
            )


# ═════════════════════════════════════════════════════════════════
#  Strategy: PR with File Patches (via Git Trees API)
# ═════════════════════════════════════════════════════════════════


async def _create_pr_with_patches(
    repo: str,
    run_id: int,
    base_branch: str,
    fix: FixProposal,
    token: str,
) -> ExecutionResult:
    """
    Create a PR using the GitHub Git Trees API (no local git clone needed).

    Flow:
      1. GET /repos/{repo}/git/ref/heads/{base_branch}     → base SHA
      2. GET /repos/{repo}/git/commits/{sha}                → base tree SHA
      3. POST /repos/{repo}/git/blobs                       → create blobs for each file
      4. POST /repos/{repo}/git/trees                       → create new tree
      5. POST /repos/{repo}/git/commits                     → create commit
      6. POST /repos/{repo}/git/refs                        → create branch ref
      7. POST /repos/{repo}/pulls                           → open PR
    """
    hdrs = _headers(token)
    fix_branch = f"devops-agent/fix-{run_id}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        # ── 1. Get base branch HEAD ──────────────────────────
        ref_url = f"{GITHUB_API}/repos/{repo}/git/ref/heads/{base_branch}"
        ref_resp = await client.get(ref_url, headers=hdrs)
        ref_resp.raise_for_status()
        base_sha = ref_resp.json()["object"]["sha"]

        # ── 2. Get base commit tree ──────────────────────────
        commit_url = f"{GITHUB_API}/repos/{repo}/git/commits/{base_sha}"
        commit_resp = await client.get(commit_url, headers=hdrs)
        commit_resp.raise_for_status()
        base_tree_sha = commit_resp.json()["tree"]["sha"]

        # ── 3. Create blobs for each patched file ────────────
        tree_items: list[dict] = []
        for filepath, content in fix.file_patches.items():
            blob_url = f"{GITHUB_API}/repos/{repo}/git/blobs"
            blob_resp = await client.post(
                blob_url,
                headers=hdrs,
                json={
                    "content": base64.b64encode(content.encode("utf-8")).decode(
                        "ascii"
                    ),
                    "encoding": "base64",
                },
            )
            blob_resp.raise_for_status()
            blob_sha = blob_resp.json()["sha"]

            tree_items.append(
                {
                    "path": filepath,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_sha,
                }
            )

        # ── 4. Create new tree ───────────────────────────────
        tree_url = f"{GITHUB_API}/repos/{repo}/git/trees"
        tree_resp = await client.post(
            tree_url,
            headers=hdrs,
            json={"base_tree": base_tree_sha, "tree": tree_items},
        )
        tree_resp.raise_for_status()
        new_tree_sha = tree_resp.json()["sha"]

        # ── 5. Create commit ─────────────────────────────────
        new_commit_url = f"{GITHUB_API}/repos/{repo}/git/commits"
        commit_message = (
            f"fix(ci): auto-remediation for run #{run_id}\n\n"
            f"{fix.description}\n\n"
            f"Risk: {fix.risk_level.value} | "
            f"Success probability: {fix.success_probability:.0%}\n\n"
            f"Applied by DevOps Pipeline Agent 🤖"
        )
        new_commit_resp = await client.post(
            new_commit_url,
            headers=hdrs,
            json={
                "message": commit_message,
                "tree": new_tree_sha,
                "parents": [base_sha],
            },
        )
        new_commit_resp.raise_for_status()
        new_commit_sha = new_commit_resp.json()["sha"]

        # ── 6. Create branch reference ───────────────────────
        ref_create_url = f"{GITHUB_API}/repos/{repo}/git/refs"
        ref_create_resp = await client.post(
            ref_create_url,
            headers=hdrs,
            json={"ref": f"refs/heads/{fix_branch}", "sha": new_commit_sha},
        )
        if ref_create_resp.status_code == 422:
            # Branch already exists — update it
            ref_update_url = f"{GITHUB_API}/repos/{repo}/git/refs/heads/{fix_branch}"
            await client.patch(
                ref_update_url,
                headers=hdrs,
                json={"sha": new_commit_sha, "force": True},
            )
        else:
            ref_create_resp.raise_for_status()

        # ── 7. Open Pull Request ─────────────────────────────
        pr_url = await _open_pull_request(
            client=client,
            repo=repo,
            head=fix_branch,
            base=base_branch,
            run_id=run_id,
            fix=fix,
            token=token,
        )

        logger.info(
            "PR created via Git Trees API — branch=%s sha=%s",
            fix_branch,
            new_commit_sha[:8],
        )

        return ExecutionResult(
            success=True,
            action_taken="pr_created",
            github_url=pr_url,
            details={
                "branch": fix_branch,
                "commit_sha": new_commit_sha,
                "files_changed": list(fix.file_patches.keys()),
            },
        )


# ═════════════════════════════════════════════════════════════════
#  Strategy: PR with Commands (documented, not executed)
# ═════════════════════════════════════════════════════════════════


async def _create_pr_with_commands(
    repo: str,
    run_id: int,
    base_branch: str,
    fix: FixProposal,
    token: str,
) -> ExecutionResult:
    """
    Create a documentation-only PR when the fix involves shell commands
    but no file patches. The commands are listed in the PR body for
    a human to review and execute manually.

    Also creates a `.devops-agent/fix-{run_id}.md` file in the repo
    as a record of the proposed fix.
    """
    fix_doc = (
        f"# DevOps Agent Fix Proposal — Run #{run_id}\n\n"
        f"**Description:** {fix.description}\n\n"
        f"**Risk Level:** {fix.risk_level.value}\n"
        f"**Success Probability:** {fix.success_probability:.0%}\n\n"
        f"## Commands to Execute\n\n"
        f"```bash\n" + "\n".join(fix.commands) + "\n```\n\n"
        f"---\n"
        f"*Generated by DevOps Pipeline Agent at "
        f"{datetime.now(timezone.utc).isoformat()}*\n"
    )

    # Reuse the patch PR flow with a single documentation file
    doc_fix = FixProposal(
        description=fix.description,
        commands=fix.commands,
        file_patches={f".devops-agent/fix-{run_id}.md": fix_doc},
        risk_level=fix.risk_level,
        success_probability=fix.success_probability,
    )

    return await _create_pr_with_patches(
        repo=repo,
        run_id=run_id,
        base_branch=base_branch,
        fix=doc_fix,
        token=token,
    )


# ═════════════════════════════════════════════════════════════════
#  Helpers
# ═════════════════════════════════════════════════════════════════


def _is_retry_fix(fix: FixProposal) -> bool:
    """Determine if the fix is a simple pipeline retry."""
    # If there are file patches, it's NEVER a simple retry
    if fix.file_patches:
        return False

    retry_keywords = {"retry", "rerun", "re-run", "re-trigger", "restart"}

    desc_lower = fix.description.lower()
    if any(kw in desc_lower for kw in retry_keywords):
        # Make sure it's ONLY a retry — no meaningful commands
        if not fix.commands:
            return True
        # Commands that are just "retry" instructions
        if all(any(kw in cmd.lower() for kw in retry_keywords) for cmd in fix.commands):
            return True

    return False


async def _open_pull_request(
    client: httpx.AsyncClient,
    repo: str,
    head: str,
    base: str,
    run_id: int,
    fix: FixProposal,
    token: str,
) -> str | None:
    """Open a GitHub Pull Request."""
    url = f"{GITHUB_API}/repos/{repo}/pulls"

    body = (
        f"## 🤖 Automated Fix — Run #{run_id}\n\n"
        f"**Description:** {fix.description}\n\n"
        f"**Risk Level:** `{fix.risk_level.value}`\n"
        f"**Success Probability:** {fix.success_probability:.0%}\n\n"
    )

    if fix.commands:
        body += "### Commands\n```bash\n" + "\n".join(fix.commands) + "\n```\n\n"

    if fix.file_patches:
        body += "### Files Changed\n"
        for fp in fix.file_patches:
            body += f"- `{fp}`\n"
        body += "\n"

    body += (
        "---\n"
        "*This PR was created automatically by the DevOps Pipeline Agent.*\n"
        "*Please review carefully before merging.*"
    )

    response = await client.post(
        url,
        headers=_headers(token),
        json={
            "title": f"🤖 fix(ci): auto-remediation for run #{run_id}",
            "body": body,
            "head": head,
            "base": base,
        },
    )

    if response.status_code == 201:
        pr_url = response.json().get("html_url", "")
        logger.info("PR opened: %s", pr_url)
        return pr_url
    elif response.status_code == 422:
        # PR may already exist
        logger.warning("PR may already exist: %s", response.text[:200])
        return f"https://github.com/{repo}/compare/{base}...{head}"
    else:
        logger.error(
            "PR creation failed: %d %s", response.status_code, response.text[:200]
        )
        return None
