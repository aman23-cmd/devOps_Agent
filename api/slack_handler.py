"""
Slack Interactivity Handler — Processes button clicks from Slack alerts.

POST /slack/interact endpoint that handles:
  • "apply_fix"      → execute fix via fix_executor, then validate
  • "retry_pipeline" → rerun failed jobs via GitHub API
  • Updates original message to "Processing..." while action runs
  • Sends result update after action completes
  • Kicks off async validation to check if the fix worked
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from agents.fix_executor import ExecutionResult, execute_fix
from agents.slack_notifier import SlackNotifier
from agents.validator import FixValidator
from api.models import FixProposal, RiskLevel
from db.fix_history import FixHistoryRecord, FixOutcome, get_session
from config.settings import get_settings

logger = logging.getLogger("slack_handler")

router = APIRouter(prefix="/slack", tags=["slack"])


# ═════════════════════════════════════════════════════════════════
#  Signature verification
# ═════════════════════════════════════════════════════════════════


def _verify_slack_signature(
    body: bytes,
    timestamp: str,
    signature: str,
    signing_secret: str,
) -> bool:
    """
    Verify Slack request signature (HMAC-SHA256).

    Includes replay protection: rejects requests older than 5 minutes.
    """
    try:
        if abs(time.time() - int(timestamp)) > 300:
            logger.warning("Slack request timestamp too old — possible replay attack")
            return False
    except (ValueError, TypeError):
        return False

    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    computed = (
        "v0="
        + hmac.new(
            signing_secret.encode("utf-8"),
            sig_basestring.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    )

    return hmac.compare_digest(computed, signature)


# ═════════════════════════════════════════════════════════════════
#  Main endpoint
# ═════════════════════════════════════════════════════════════════


@router.post("/interact")
async def handle_slack_interaction(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """
    Handle interactive component payloads from Slack.

    Slack sends a POST with a URL-encoded `payload` field containing JSON.
    We must respond within 3 seconds, so heavy work is done in background tasks.
    """
    settings = get_settings()
    raw_body = await request.body()

    # ── Verify signature ─────────────────────────────────────
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "0")
    signature = request.headers.get("X-Slack-Signature", "")

    if not _verify_slack_signature(
        raw_body,
        timestamp,
        signature,
        settings.SLACK_SIGNING_SECRET,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Slack signature",
        )

    # ── Parse payload ────────────────────────────────────────
    form_data = await request.form()
    payload = json.loads(form_data.get("payload", "{}"))

    actions = payload.get("actions", [])
    if not actions:
        return {"ok": True}

    action = actions[0]
    action_id = action.get("action_id", "")
    action_value = json.loads(action.get("value", "{}"))
    user = payload.get("user", {}).get("username", "unknown")
    channel = payload.get("channel", {}).get("id")
    message_ts = payload.get("message", {}).get("ts")

    run_id = action_value.get("run_id")
    repo = action_value.get("repo")
    branch = action_value.get("branch", "main")

    logger.info(
        "Slack interaction — action=%s run_id=%s repo=%s user=%s",
        action_id,
        run_id,
        repo,
        user,
    )

    # ── Route to handler ─────────────────────────────────────
    if action_id == "apply_fix":
        # Respond immediately, process in background
        background_tasks.add_task(
            _handle_apply_fix,
            run_id,
            repo,
            branch,
            user,
            channel,
            message_ts,
        )
        return {"ok": True}

    elif action_id == "retry_pipeline":
        background_tasks.add_task(
            _handle_retry_pipeline,
            run_id,
            repo,
            branch,
            user,
            channel,
            message_ts,
        )
        return {"ok": True}

    elif action_id == "view_logs":
        # Link buttons are handled client-side by Slack — no server action needed
        return {"ok": True}

    else:
        logger.warning("Unknown action_id: %s", action_id)
        return {"ok": True}


# ═════════════════════════════════════════════════════════════════
#  Action: Apply Fix
# ═════════════════════════════════════════════════════════════════


async def _handle_apply_fix(
    run_id: int,
    repo: str,
    branch: str,
    user: str,
    channel: str | None,
    message_ts: str | None,
) -> None:
    """
    Process "Apply Suggested Fix" button click.

    Steps:
      1. Update message to "Processing..."
      2. Fetch fix details from DB
      3. Execute fix via fix_executor
      4. Post result to thread
      5. Start async validation
    """
    notifier = SlackNotifier()

    # ── 1. Show processing state ─────────────────────────────
    if message_ts:
        await notifier.send_thread_update(
            thread_ts=message_ts,
            message=f"✅ Fix approved by @{user} — applying now...",
            channel_id=channel,
        )

    # ── 2. Fetch fix details from DB ─────────────────────────
    session = get_session()
    try:
        record = (
            session.query(FixHistoryRecord)
            .filter_by(run_id=run_id)
            .order_by(FixHistoryRecord.created_at.desc())
            .first()
        )

        if not record:
            logger.error("No DB record for run_id=%d", run_id)
            if message_ts:
                await notifier.send_thread_update(
                    thread_ts=message_ts,
                    message="❌ Error: No fix record found in database.",
                    channel_id=channel,
                )
            return

        # Check if already processed
        if record.fix_outcome not in (FixOutcome.PENDING, FixOutcome.PENDING.value):
            if message_ts:
                await notifier.send_thread_update(
                    thread_ts=message_ts,
                    message=f"⚠️ This fix was already processed (outcome: {record.fix_outcome})",
                    channel_id=channel,
                )
            return

        # Reconstruct fix proposal
        fix = FixProposal(
            description=record.fix_applied or "No description",
            commands=json.loads(record.fix_commands) if record.fix_commands else [],
            risk_level=RiskLevel(record.risk_level or "HIGH"),
            success_probability=0.0,
        )

    except Exception as exc:
        logger.error("DB read failed: %s", exc)
        return
    finally:
        session.close()

    # ── 3. Execute fix ───────────────────────────────────────
    try:
        result = await execute_fix(
            fix_proposal=fix,
            run_id=run_id,
            repo=repo,
            base_branch=branch,
        )
    except Exception as exc:
        logger.error("Fix execution failed: %s", exc, exc_info=True)
        if message_ts:
            await notifier.send_fix_outcome(
                thread_ts=message_ts,
                success=False,
                details=f"Execution error: {str(exc)[:500]}",
                channel_id=channel,
            )
        return

    # ── 4. Post result ───────────────────────────────────────
    if message_ts:
        if result.success:
            details = f"Action: `{result.action_taken}`"
            if result.github_url:
                details += f"\n<{result.github_url}|View on GitHub>"
            await notifier.send_fix_outcome(
                thread_ts=message_ts,
                success=True,
                details=details,
                channel_id=channel,
            )
        else:
            await notifier.send_fix_outcome(
                thread_ts=message_ts,
                success=False,
                details=result.error or "Unknown error",
                channel_id=channel,
            )

    # ── 5. Start async validation ────────────────────────────
    if result.success:
        # Fire and forget — validation runs in the background
        asyncio.create_task(
            _run_validation(
                repo=repo,
                branch=branch,
                run_id=run_id,
                result=result,
                slack_ts=message_ts,
                channel_id=channel,
            )
        )


# ═════════════════════════════════════════════════════════════════
#  Action: Retry Pipeline
# ═════════════════════════════════════════════════════════════════


async def _handle_retry_pipeline(
    run_id: int,
    repo: str,
    branch: str,
    user: str,
    channel: str | None,
    message_ts: str | None,
) -> None:
    """
    Process "Retry Pipeline" button click.

    Calls GitHub API to rerun failed jobs.
    """
    notifier = SlackNotifier()

    if message_ts:
        await notifier.send_thread_update(
            thread_ts=message_ts,
            message=f"🔄 Pipeline retry requested by @{user}...",
            channel_id=channel,
        )

    # Create a simple retry fix proposal
    retry_fix = FixProposal(
        description="Retry the failed pipeline run",
        commands=["retry"],
        risk_level=RiskLevel.LOW,
        success_probability=0.5,
    )

    try:
        result = await execute_fix(
            fix_proposal=retry_fix,
            run_id=run_id,
            repo=repo,
            base_branch=branch,
        )
    except Exception as exc:
        logger.error("Retry failed: %s", exc)
        if message_ts:
            await notifier.send_fix_outcome(
                thread_ts=message_ts,
                success=False,
                details=f"Retry error: {str(exc)[:500]}",
                channel_id=channel,
            )
        return

    if message_ts:
        if result.success:
            await notifier.send_thread_update(
                thread_ts=message_ts,
                message=(
                    f"🔄 Pipeline retry triggered!\n"
                    f"<{result.github_url}|View run on GitHub>"
                ),
                channel_id=channel,
            )
        else:
            await notifier.send_fix_outcome(
                thread_ts=message_ts,
                success=False,
                details=result.error or "Retry failed",
                channel_id=channel,
            )

    # Start validation for the retry
    if result.success:
        asyncio.create_task(
            _run_validation(
                repo=repo,
                branch=branch,
                run_id=run_id,
                result=result,
                slack_ts=message_ts,
                channel_id=channel,
            )
        )


# ═════════════════════════════════════════════════════════════════
#  Async validation runner
# ═════════════════════════════════════════════════════════════════


async def _run_validation(
    repo: str,
    branch: str,
    run_id: int,
    result: ExecutionResult,
    slack_ts: str | None,
    channel_id: str | None,
) -> None:
    """Run the FixValidator as a background task."""
    try:
        validator = FixValidator()
        await validator.validate_fix(
            repo=repo,
            branch=branch,
            run_id=run_id,
            execution_result=result,
            slack_ts=slack_ts,
            channel_id=channel_id,
        )
    except Exception as exc:
        logger.error("Validation task failed: %s", exc, exc_info=True)
