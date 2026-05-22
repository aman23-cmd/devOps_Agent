"""
Log Fetcher Agent — AutoGen UserProxyAgent with GitHub log retrieval tool.

This agent is registered with a tool function that:
  1. Calls GitHub REST API to download workflow run logs (ZIP)
  2. Extracts per-job text files from the archive
  3. Applies smart truncation:
     - Last 100 lines for PASSING steps (context)
     - FULL output for FAILED steps
     - Hard cap at MAX_LOG_LINES total (default 5000)
  4. Identifies the failed step name, error message, and stack trace
  5. Returns structured dict for downstream agents

Runs as a UserProxyAgent inside the AutoGen GroupChat.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from typing import Any, Optional

import httpx

from config.settings import get_settings

logger = logging.getLogger("log_fetcher_agent")

# ── Regex patterns for error detection ───────────────────────────
ERROR_PATTERNS = re.compile(
    r"(error|fail(ed|ure)?|fatal|exception|traceback|denied|refused|timeout|"
    r"cannot|could not|unable to|not found|exit code [1-9]|ENOENT|"
    r"ModuleNotFoundError|ImportError|SyntaxError|TypeError|"
    r"npm ERR!|pip.*error|docker.*error|permission denied|"
    r"FAILED|AssertionError|RuntimeError|ValueError|KeyError|"
    r"OOMKilled|MemoryError|ConnectionRefusedError)",
    re.IGNORECASE,
)

STACK_TRACE_START = re.compile(
    r"(Traceback \(most recent call last\)|"
    r"at\s+\S+\s+\(\S+:\d+:\d+\)|"  # JS stack trace
    r"^\s+File\s+\".*\",\s+line\s+\d+|"  # Python stack trace
    r"panic:|FATAL ERROR)",
    re.MULTILINE,
)


# ── The tool function registered with the UserProxyAgent ─────────


async def fetch_github_logs(
    run_id: int,
    repo: str,
    token: Optional[str] = None,
) -> dict[str, Any]:
    """
    Fetch and parse GitHub Actions workflow logs for a failed run.

    This function is registered as a tool on the Log Fetcher
    UserProxyAgent within the AutoGen GroupChat.

    Args:
        run_id: GitHub Actions workflow run ID
        repo: Full repository name (e.g. "owner/repo")
        token: GitHub PAT (falls back to env GITHUB_TOKEN)

    Returns:
        {
            "failed_step": str,
            "error_message": str,
            "truncated_logs": str,
            "stack_trace": str | None,
            "total_lines": int,
            "kept_lines": int,
            "job_names": list[str],
            "failed_jobs": list[dict],
        }
    """
    settings = get_settings()
    gh_token = token or settings.GITHUB_TOKEN
    max_lines = settings.MAX_LOG_LINES

    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # ── 1. Fetch failed job metadata ─────────────────────────
    failed_jobs = await _fetch_failed_jobs(repo, run_id, headers)

    # ── 2. Download log archive ──────────────────────────────
    log_url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/logs"

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        response = await client.get(log_url, headers=headers)

        if response.status_code == 404:
            logger.warning("Logs expired or not found — run_id=%d", run_id)
            return {
                "failed_step": "unknown",
                "error_message": "Logs not available (expired or deleted)",
                "truncated_logs": "[No logs available]",
                "stack_trace": None,
                "total_lines": 0,
                "kept_lines": 0,
                "job_names": [],
                "failed_jobs": failed_jobs,
            }

        response.raise_for_status()

    # ── 3. Extract and parse ZIP ─────────────────────────────
    job_logs, job_names = _extract_zip_archive(response.content)

    # ── 4. Identify failed steps ─────────────────────────────
    failed_step_names = set()
    for job in failed_jobs:
        failed_step_names.update(job.get("failed_steps", []))

    # ── 5. Smart truncation ──────────────────────────────────
    truncated, total_lines, kept_lines = _smart_truncate(
        job_logs=job_logs,
        failed_step_names=failed_step_names,
        max_lines=max_lines,
    )

    # ── 6. Extract error message and stack trace ─────────────
    error_message = _extract_primary_error(truncated)
    stack_trace = _extract_stack_trace(truncated)

    # ── 7. Determine the failed step name ────────────────────
    failed_step = "unknown"
    if failed_step_names:
        failed_step = ", ".join(sorted(failed_step_names))
    elif failed_jobs:
        failed_step = failed_jobs[0].get("name", "unknown")

    logger.info(
        "Logs fetched — run_id=%d total=%d kept=%d failed_step=%s",
        run_id,
        total_lines,
        kept_lines,
        failed_step,
    )

    return {
        "failed_step": failed_step,
        "error_message": error_message,
        "truncated_logs": truncated,
        "stack_trace": stack_trace,
        "total_lines": total_lines,
        "kept_lines": kept_lines,
        "job_names": job_names,
        "failed_jobs": failed_jobs,
    }


# ── Helper: Fetch failed jobs metadata ───────────────────────────


async def _fetch_failed_jobs(
    repo: str,
    run_id: int,
    headers: dict[str, str],
) -> list[dict]:
    """Query the GitHub Jobs API for failed jobs in this run."""
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()

        failed = []
        for job in response.json().get("jobs", []):
            if job.get("conclusion") == "failure":
                failed.append(
                    {
                        "job_id": job["id"],
                        "name": job["name"],
                        "conclusion": job["conclusion"],
                        "started_at": job.get("started_at"),
                        "completed_at": job.get("completed_at"),
                        "failed_steps": [
                            step["name"]
                            for step in job.get("steps", [])
                            if step.get("conclusion") == "failure"
                        ],
                    }
                )
        return failed

    except Exception as exc:
        logger.warning("Could not fetch job metadata: %s", exc)
        return []


# ── Helper: Extract ZIP archive ──────────────────────────────────


def _extract_zip_archive(
    zip_bytes: bytes,
) -> tuple[dict[str, list[str]], list[str]]:
    """
    Extract the GitHub log ZIP into per-file line lists.

    Returns:
        (job_logs, job_names) where job_logs is {filename: [lines]}
    """
    job_logs: dict[str, list[str]] = {}
    job_names: list[str] = []

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in sorted(zf.namelist()):
            if name.endswith("/"):
                continue

            job_name = name.rsplit("/", 1)[0] if "/" in name else name
            if job_name not in job_names:
                job_names.append(job_name)

            try:
                raw = zf.read(name).decode("utf-8", errors="replace")
                # Strip GitHub timestamp prefixes
                cleaned = re.sub(
                    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ",
                    "",
                    raw,
                    flags=re.MULTILINE,
                )
                job_logs[name] = cleaned.split("\n")
            except Exception as exc:
                job_logs[name] = [f"[Decode error: {exc}]"]

    return job_logs, job_names


# ── Helper: Smart truncation ────────────────────────────────────


def _smart_truncate(
    job_logs: dict[str, list[str]],
    failed_step_names: set[str],
    max_lines: int,
) -> tuple[str, int, int]:
    """
    Truncation strategy:
      - FAILED steps: keep FULL output
      - PASSING steps: keep only last 100 lines (context)
      - Hard cap at max_lines total

    Returns: (truncated_text, total_lines, kept_lines)
    """
    PASSING_TAIL = 100
    sections: list[str] = []
    total_lines = 0
    kept_lines = 0

    for filename, lines in job_logs.items():
        total_lines += len(lines)

        # Determine if this file corresponds to a failed step
        is_failed = any(step_name in filename for step_name in failed_step_names)

        header = f"\n{'=' * 60}\n  LOG: {filename}"
        if is_failed:
            header += " [FAILED ❌]"
        header += f"\n{'=' * 60}\n"

        if is_failed:
            # Keep full output for failed steps
            section_lines = lines
        else:
            # Keep only last N lines for passing steps
            if len(lines) > PASSING_TAIL:
                section_lines = [
                    f"... [{len(lines) - PASSING_TAIL} lines omitted (passing step)] ..."
                ] + lines[-PASSING_TAIL:]
            else:
                section_lines = lines

        sections.append(header + "\n".join(section_lines))
        kept_lines += len(section_lines)

    combined = "\n".join(sections)
    combined_lines = combined.split("\n")

    # Hard cap
    if len(combined_lines) > max_lines:
        # Prioritise the end (where errors usually are)
        head_budget = max_lines // 5
        tail_budget = max_lines - head_budget
        combined = "\n".join(
            combined_lines[:head_budget]
            + [
                f"\n... [{len(combined_lines) - max_lines} lines truncated to fit {max_lines} line cap] ...\n"
            ]
            + combined_lines[-tail_budget:]
        )
        kept_lines = max_lines

    return combined, total_lines, kept_lines


# ── Helper: Extract primary error ────────────────────────────────


def _extract_primary_error(logs: str) -> str:
    """Pull the most relevant error message from the logs."""
    lines = logs.split("\n")
    error_lines: list[str] = []

    for line in lines:
        if ERROR_PATTERNS.search(line):
            cleaned = line.strip()
            if cleaned and len(cleaned) > 10:
                error_lines.append(cleaned)

    if not error_lines:
        return "No clear error message found in logs"

    # Return the last error (usually the most specific)
    # but cap at 500 chars
    primary = error_lines[-1]
    return primary[:500] if len(primary) > 500 else primary


# ── Helper: Extract stack trace ──────────────────────────────────


def _extract_stack_trace(logs: str) -> str | None:
    """Extract the first complete stack trace from the logs."""
    match = STACK_TRACE_START.search(logs)
    if not match:
        return None

    start_pos = match.start()
    # Grab up to 50 lines from the stack trace start
    trace_text = logs[start_pos:]
    trace_lines = trace_text.split("\n")[:50]
    return "\n".join(trace_lines)
