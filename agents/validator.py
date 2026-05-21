"""
Validator Agent — Post-fix verification via GitHub API polling.

After a fix is applied, this agent:
  1. Polls GitHub API every 30 seconds for a new workflow run on the same branch
  2. Times out after 10 minutes
  3. On success → sends resolution message, updates DB with outcome="success"
  4. On failure → escalates to human, updates DB with outcome="failure"
  5. If 3 consecutive failures in the same category → sends PagerDuty alert

This closes the feedback loop: the agent learns whether its fixes actually work.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from agents.slack_notifier import SlackNotifier
from agents.fix_executor import ExecutionResult
from db.fix_history import FixHistoryRecord, FixOutcome, get_session
from config.settings import get_settings

logger = logging.getLogger("validator")

# ── Configuration ────────────────────────────────────────────────
POLL_INTERVAL_SECONDS = 30
POLL_TIMEOUT_SECONDS = 600  # 10 minutes
CONSECUTIVE_FAILURE_THRESHOLD = 3

GITHUB_API = "https://api.github.com"


class FixValidator:
    """
    Polls GitHub Actions to verify whether an applied fix resolved the failure.

    Usage:
        validator = FixValidator()
        outcome = await validator.validate_fix(
            repo="owner/repo",
            branch="main",
            run_id=12345,
            execution_result=exec_result,
            slack_ts="1234567890.123456",
        )
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._token = settings.GITHUB_TOKEN
        self._notifier = SlackNotifier()
        self._pagerduty_key: str | None = getattr(settings, "PAGERDUTY_ROUTING_KEY", None)

    async def validate_fix(
        self,
        repo: str,
        branch: str,
        run_id: int,
        execution_result: ExecutionResult,
        slack_ts: str | None = None,
        channel_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Poll GitHub for a new workflow run triggered by the fix.

        Args:
            repo: Full repo name (owner/repo)
            branch: Branch the fix was applied to
            run_id: Original failed run ID
            execution_result: Result from fix_executor
            slack_ts: Slack message timestamp for threading
            channel_id: Slack channel ID

        Returns:
            {
                "validated": bool,
                "new_run_id": int | None,
                "new_conclusion": str | None,
                "time_to_fix": str,
                "error": str | None,
            }
        """
        logger.info(
            "Starting fix validation — repo=%s branch=%s original_run=%d",
            repo, branch, run_id,
        )

        if slack_ts:
            await self._notifier.send_thread_update(
                thread_ts=slack_ts,
                message="🔎 Monitoring for new pipeline run...",
                channel_id=channel_id,
            )

        start_time = datetime.now(timezone.utc)
        original_run_created_at = await self._get_run_created_at(repo, run_id)

        # ── Poll loop ────────────────────────────────────────
        elapsed = 0
        new_run: dict | None = None

        while elapsed < POLL_TIMEOUT_SECONDS:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS

            logger.debug(
                "Polling GitHub — elapsed=%ds/%ds",
                elapsed, POLL_TIMEOUT_SECONDS,
            )

            new_run = await self._find_new_run(
                repo=repo,
                branch=branch,
                after=original_run_created_at or start_time,
                exclude_run_id=run_id,
            )

            if new_run is None:
                continue

            conclusion = new_run.get("conclusion")
            new_run_id = new_run.get("id")
            status = new_run.get("status")

            # Run found but still in progress
            if status != "completed":
                logger.info(
                    "New run found (id=%d) but status=%s — waiting...",
                    new_run_id, status,
                )
                continue

            # Run completed — evaluate outcome
            fix_duration = datetime.now(timezone.utc) - start_time
            time_to_fix = _format_duration(fix_duration.total_seconds())
            detect_duration = (start_time - (original_run_created_at or start_time))
            time_to_detect = _format_duration(detect_duration.total_seconds())

            if conclusion == "success":
                logger.info(
                    "✅ Fix validated — new run %d succeeded in %s",
                    new_run_id, time_to_fix,
                )
                await self._handle_success(
                    repo=repo,
                    run_id=run_id,
                    new_run_id=new_run_id,
                    time_to_detect=time_to_detect,
                    time_to_fix=time_to_fix,
                    execution_result=execution_result,
                    slack_ts=slack_ts,
                    channel_id=channel_id,
                )
                return {
                    "validated": True,
                    "new_run_id": new_run_id,
                    "new_conclusion": "success",
                    "time_to_fix": time_to_fix,
                    "error": None,
                }

            else:
                logger.warning(
                    "❌ Fix did not resolve the issue — new run %d concluded: %s",
                    new_run_id, conclusion,
                )
                await self._handle_failure(
                    repo=repo,
                    run_id=run_id,
                    new_run_id=new_run_id,
                    conclusion=conclusion,
                    slack_ts=slack_ts,
                    channel_id=channel_id,
                )
                return {
                    "validated": False,
                    "new_run_id": new_run_id,
                    "new_conclusion": conclusion,
                    "time_to_fix": time_to_fix,
                    "error": f"New run concluded with: {conclusion}",
                }

        # ── Timeout ──────────────────────────────────────────
        logger.warning("Validation timed out after %ds", POLL_TIMEOUT_SECONDS)
        if slack_ts:
            await self._notifier.send_thread_update(
                thread_ts=slack_ts,
                message=(
                    f"⏰ *Validation timed out* after {POLL_TIMEOUT_SECONDS // 60} minutes.\n"
                    f"No new workflow run detected on branch `{branch}`.\n"
                    f"Please check manually."
                ),
                channel_id=channel_id,
            )

        return {
            "validated": False,
            "new_run_id": None,
            "new_conclusion": None,
            "time_to_fix": f"{POLL_TIMEOUT_SECONDS // 60}m (timeout)",
            "error": "Validation timed out",
        }

    # ═════════════════════════════════════════════════════════
    #  Success handler
    # ═════════════════════════════════════════════════════════

    async def _handle_success(
        self,
        repo: str,
        run_id: int,
        new_run_id: int,
        time_to_detect: str,
        time_to_fix: str,
        execution_result: ExecutionResult,
        slack_ts: str | None,
        channel_id: str | None,
    ) -> None:
        """Update DB and Slack after successful fix validation."""
        # Update DB
        session = get_session()
        try:
            record = (
                session.query(FixHistoryRecord)
                .filter_by(run_id=run_id)
                .order_by(FixHistoryRecord.created_at.desc())
                .first()
            )
            if record:
                record.fix_outcome = FixOutcome.SUCCESS
                record.resolved_at = datetime.now(timezone.utc)
                session.commit()
                logger.info("DB updated — run_id=%d outcome=success", run_id)
        except Exception as exc:
            session.rollback()
            logger.error("DB update failed: %s", exc)
        finally:
            session.close()

        # Send resolution to Slack
        if slack_ts:
            await self._notifier.send_resolution(
                channel_id=channel_id,
                message_ts=slack_ts,
                outcome_data={
                    "time_to_detect": time_to_detect,
                    "time_to_fix": time_to_fix,
                    "fix_method": execution_result.action_taken,
                    "github_url": execution_result.github_url,
                    "details": (
                        f"Original run: #{run_id}\n"
                        f"Verification run: #{new_run_id}\n"
                        f"Fix: {execution_result.action_taken}"
                    ),
                },
            )

    # ═════════════════════════════════════════════════════════
    #  Failure handler
    # ═════════════════════════════════════════════════════════

    async def _handle_failure(
        self,
        repo: str,
        run_id: int,
        new_run_id: int,
        conclusion: str,
        slack_ts: str | None,
        channel_id: str | None,
    ) -> None:
        """Update DB, escalate via Slack, check for repeated failures."""
        # Update DB
        session = get_session()
        category: str | None = None
        try:
            record = (
                session.query(FixHistoryRecord)
                .filter_by(run_id=run_id)
                .order_by(FixHistoryRecord.created_at.desc())
                .first()
            )
            if record:
                record.fix_outcome = FixOutcome.FAILURE
                record.resolved_at = datetime.now(timezone.utc)
                category = record.root_cause_category
                session.commit()
        except Exception as exc:
            session.rollback()
            logger.error("DB update failed: %s", exc)
        finally:
            session.close()

        # Escalate via Slack
        if slack_ts:
            await self._notifier.send_fix_outcome(
                thread_ts=slack_ts,
                success=False,
                details=(
                    f"The fix did not resolve the issue.\n"
                    f"New run #{new_run_id} concluded with: `{conclusion}`.\n"
                    f"Manual intervention required."
                ),
                channel_id=channel_id,
            )

        # Check for consecutive failures
        if category:
            await self._check_consecutive_failures(
                repo=repo,
                category=category,
                slack_ts=slack_ts,
                channel_id=channel_id,
            )

    # ═════════════════════════════════════════════════════════
    #  Consecutive failure detection + PagerDuty
    # ═════════════════════════════════════════════════════════

    async def _check_consecutive_failures(
        self,
        repo: str,
        category: str,
        slack_ts: str | None,
        channel_id: str | None,
    ) -> None:
        """
        Check if the last N fixes for this category all failed.
        If so, trigger PagerDuty and post an escalation to Slack.
        """
        session = get_session()
        try:
            recent = (
                session.query(FixHistoryRecord)
                .filter_by(repo=repo, root_cause_category=category)
                .order_by(FixHistoryRecord.created_at.desc())
                .limit(CONSECUTIVE_FAILURE_THRESHOLD)
                .all()
            )

            if len(recent) < CONSECUTIVE_FAILURE_THRESHOLD:
                return

            all_failed = all(
                r.fix_outcome in (FixOutcome.FAILURE, FixOutcome.FAILURE.value)
                for r in recent
            )

            if not all_failed:
                return

            logger.critical(
                "🚨 %d consecutive failures for category=%s repo=%s — escalating!",
                CONSECUTIVE_FAILURE_THRESHOLD, category, repo,
            )

            # PagerDuty alert
            await self._send_pagerduty_alert(repo=repo, category=category)

            # Slack escalation
            if slack_ts:
                await self._notifier.send_escalation(
                    channel_id=channel_id,
                    thread_ts=slack_ts,
                    repo=repo,
                    category=category,
                    consecutive_failures=CONSECUTIVE_FAILURE_THRESHOLD,
                    message=(
                        "Automated fixes have failed repeatedly. "
                        "PagerDuty alert has been triggered. "
                        "This requires immediate human attention."
                    ),
                )

        except Exception as exc:
            logger.error("Consecutive failure check failed: %s", exc)
        finally:
            session.close()

    async def _send_pagerduty_alert(
        self,
        repo: str,
        category: str,
    ) -> None:
        """
        Send an incident to PagerDuty via Events API v2.

        Requires PAGERDUTY_ROUTING_KEY environment variable.
        """
        if not self._pagerduty_key:
            logger.warning("PagerDuty routing key not configured — skipping alert")
            return

        url = "https://events.pagerduty.com/v2/enqueue"
        payload = {
            "routing_key": self._pagerduty_key,
            "event_action": "trigger",
            "payload": {
                "summary": (
                    f"DevOps Agent: {CONSECUTIVE_FAILURE_THRESHOLD} consecutive "
                    f"'{category}' fix failures in {repo}"
                ),
                "severity": "critical",
                "source": "devops-pipeline-agent",
                "component": repo,
                "group": category,
                "class": "ci_cd_failure",
                "custom_details": {
                    "repo": repo,
                    "category": category,
                    "consecutive_failures": CONSECUTIVE_FAILURE_THRESHOLD,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            },
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(url, json=payload)
                if response.status_code == 202:
                    logger.info("PagerDuty alert sent — repo=%s category=%s", repo, category)
                else:
                    logger.error(
                        "PagerDuty alert failed: %d %s",
                        response.status_code, response.text[:200],
                    )
        except Exception as exc:
            logger.error("PagerDuty request failed: %s", exc)

    # ═════════════════════════════════════════════════════════
    #  GitHub API helpers
    # ═════════════════════════════════════════════════════════

    async def _get_run_created_at(
        self,
        repo: str,
        run_id: int,
    ) -> datetime | None:
        """Get the created_at timestamp of a workflow run."""
        url = f"{GITHUB_API}/repos/{repo}/actions/runs/{run_id}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github.v3+json",
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                created_at_str = response.json().get("created_at", "")
                return datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        except Exception as exc:
            logger.warning("Could not get run created_at: %s", exc)
            return None

    async def _find_new_run(
        self,
        repo: str,
        branch: str,
        after: datetime,
        exclude_run_id: int,
    ) -> dict | None:
        """
        Find a new workflow run on the same branch created after the given timestamp.
        """
        url = f"{GITHUB_API}/repos/{repo}/actions/runs"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github.v3+json",
        }
        params = {
            "branch": branch,
            "per_page": 5,
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url, headers=headers, params=params)
                response.raise_for_status()

            runs = response.json().get("workflow_runs", [])

            for run in runs:
                run_id = run.get("id")
                if run_id == exclude_run_id:
                    continue

                created_at_str = run.get("created_at", "")
                try:
                    created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue

                if created_at > after:
                    return run

            return None

        except Exception as exc:
            logger.warning("GitHub run lookup failed: %s", exc)
            return None


# ── Utility ──────────────────────────────────────────────────────

def _format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins}m {secs}s"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"
