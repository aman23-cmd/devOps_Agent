"""
Slack Notifier — Rich Block Kit messages for the DevOps Pipeline Agent.

Two message types:
  TYPE A — Failure + Fix Proposal (red theme)
    • Header, metadata fields, explanation, fix recommendation, action buttons
  TYPE B — Resolution (green theme)
    • Header, timing stats, fix method used

Functions:
  send_failure_alert() → posts TYPE A, returns message_ts
  send_resolution()    → posts TYPE B in thread, updates original message
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError

from api.models import (
    DiagnosisResult,
    FixProposal,
    PipelineFailureEvent,
    RiskLevel,
)
from config.settings import get_settings

logger = logging.getLogger("slack_notifier")


# ── Risk level styling ───────────────────────────────────────────
_RISK_BADGE: dict[RiskLevel, str] = {
    RiskLevel.LOW: "🟢 LOW",
    RiskLevel.MEDIUM: "🟡 MEDIUM",
    RiskLevel.HIGH: "🔴 HIGH",
}


class SlackNotifier:
    """
    Sends structured Block Kit messages to Slack.

    Usage:
        notifier = SlackNotifier()
        ts = await notifier.send_failure_alert(channel_id, event, diagnosis, fix_proposals)
        await notifier.send_resolution(channel_id, ts, outcome_data)
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._client = AsyncWebClient(token=settings.SLACK_BOT_TOKEN)
        self._default_channel = settings.SLACK_CHANNEL_ID

    # ═════════════════════════════════════════════════════════
    #  TYPE A — Failure + Fix Proposal Alert
    # ═════════════════════════════════════════════════════════

    async def send_failure_alert(
        self,
        channel_id: str | None,
        event: PipelineFailureEvent,
        diagnosis: DiagnosisResult,
        fix_proposals: list[FixProposal],
    ) -> str | None:
        """
        Post a TYPE A failure alert with diagnosis and fix proposal.

        Args:
            channel_id: Slack channel (falls back to default)
            event: Pipeline failure event metadata
            diagnosis: Root cause analysis
            fix_proposals: Ranked list of fix proposals

        Returns:
            Message timestamp (ts) for threading follow-ups, or None on failure
        """
        channel = channel_id or self._default_channel
        best_fix = fix_proposals[0] if fix_proposals else None
        run_link = event.html_url or f"https://github.com/{event.repo_full_name}/actions/runs/{event.run_id}"

        blocks: list[dict[str, Any]] = [
            # ── Header ───────────────────────────────────────
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🔴 Pipeline Failure Detected",
                    "emoji": True,
                },
            },
            # ── Metadata fields ──────────────────────────────
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Repo:*\n`{event.repo_full_name}`"},
                    {"type": "mrkdwn", "text": f"*Branch:*\n`{event.branch}`"},
                    {"type": "mrkdwn", "text": f"*Workflow:*\n`{event.workflow_name}`"},
                    {"type": "mrkdwn", "text": f"*Commit:*\n`{event.commit_sha[:8]}`"},
                    {
                        "type": "mrkdwn",
                        "text": f"*Root Cause:*\n`{diagnosis.root_cause_category.value}`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Confidence:*\n`{diagnosis.confidence:.0%}`",
                    },
                ],
            },
            {"type": "divider"},
            # ── Diagnosis explanation ────────────────────────
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*🔍 Root Cause Analysis*\n\n"
                        f"*Failed Step:* `{diagnosis.failure_step}`\n\n"
                        f"*Error:*\n```{diagnosis.error_message[:500]}```\n\n"
                        f"{diagnosis.explanation}"
                    ),
                },
            },
        ]

        # ── Contributing factors ─────────────────────────────
        if diagnosis.contributing_factors:
            factors = "\n".join(f"• {f}" for f in diagnosis.contributing_factors[:5])
            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Contributing Factors:*\n{factors}",
                    }
                ],
            })

        # ── Fix recommendation ───────────────────────────────
        if best_fix:
            risk_badge = _RISK_BADGE.get(best_fix.risk_level, "⚪ UNKNOWN")

            blocks.extend([
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*🔧 Recommended Fix*\n\n"
                            f"{best_fix.description}\n\n"
                            f"*Risk:* {risk_badge}  |  "
                            f"*Success Probability:* `{best_fix.success_probability:.0%}`"
                        ),
                    },
                },
            ])

            if best_fix.commands:
                cmd_text = "\n".join(best_fix.commands[:8])
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Commands:*\n```{cmd_text}```",
                    },
                })

            if len(fix_proposals) > 1:
                alt_text = "\n".join(
                    f"  {i}. {f.description} ({_RISK_BADGE.get(f.risk_level, '?')} — {f.success_probability:.0%})"
                    for i, f in enumerate(fix_proposals[1:], 2)
                )
                blocks.append({
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": f"*Alternative fixes:*\n{alt_text}"},
                    ],
                })

        # ── Action buttons ───────────────────────────────────
        action_payload = json.dumps({
            "run_id": event.run_id,
            "repo": event.repo_full_name,
            "branch": event.branch,
        })

        action_elements: list[dict] = [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✅ Apply Suggested Fix", "emoji": True},
                "style": "primary",
                "action_id": "apply_fix",
                "value": action_payload,
                "confirm": {
                    "title": {"type": "plain_text", "text": "Apply Fix?"},
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"This will apply a `{best_fix.risk_level.value if best_fix else 'UNKNOWN'}` "
                            f"risk fix to `{event.repo_full_name}`."
                        ),
                    },
                    "confirm": {"type": "plain_text", "text": "Apply"},
                    "deny": {"type": "plain_text", "text": "Cancel"},
                },
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🔄 Retry Pipeline", "emoji": True},
                "action_id": "retry_pipeline",
                "value": action_payload,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "📋 View Full Logs", "emoji": True},
                "url": run_link,
                "action_id": "view_logs",
            },
        ]

        blocks.append({"type": "actions", "elements": action_elements})

        # ── Timestamp footer ─────────────────────────────────
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"⏱️ Detected at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
                }
            ],
        })

        # ── Send ─────────────────────────────────────────────
        try:
            result = await self._client.chat_postMessage(
                channel=channel,
                blocks=blocks,
                text=(
                    f"🔴 Pipeline failure in {event.repo_full_name} ({event.branch}) "
                    f"— {diagnosis.root_cause_category.value}"
                ),
            )
            ts = result.get("ts")
            logger.info("Failure alert posted — ts=%s channel=%s", ts, channel)
            return ts

        except SlackApiError as exc:
            logger.error("Slack API error (failure alert): %s", exc.response["error"])
            return None

    # ═════════════════════════════════════════════════════════
    #  TYPE B — Resolution Message (green)
    # ═════════════════════════════════════════════════════════

    async def send_resolution(
        self,
        channel_id: str | None,
        message_ts: str,
        outcome_data: dict[str, Any],
    ) -> None:
        """
        Post a TYPE B resolution message in the alert thread.

        Args:
            channel_id: Slack channel
            message_ts: Original alert message timestamp (for threading)
            outcome_data: {
                "time_to_detect": str,   # e.g. "2m 30s"
                "time_to_fix": str,      # e.g. "5m 12s"
                "fix_method": str,       # e.g. "rerun_failed_jobs" | "pr_created"
                "github_url": str | None,
                "details": str,
            }
        """
        channel = channel_id or self._default_channel

        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "✅ Pipeline Fixed",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Time to Detect:*\n`{outcome_data.get('time_to_detect', 'N/A')}`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Time to Fix:*\n`{outcome_data.get('time_to_fix', 'N/A')}`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Fix Method:*\n`{outcome_data.get('fix_method', 'N/A')}`",
                    },
                ],
            },
        ]

        details = outcome_data.get("details", "")
        github_url = outcome_data.get("github_url")

        if details or github_url:
            detail_text = details
            if github_url:
                detail_text += f"\n<{github_url}|View on GitHub>"
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": detail_text},
            })

        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"🕐 Resolved at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
                }
            ],
        })

        try:
            await self._client.chat_postMessage(
                channel=channel,
                thread_ts=message_ts,
                blocks=blocks,
                text="✅ Pipeline fixed successfully",
            )
            logger.info("Resolution posted — thread_ts=%s", message_ts)

        except SlackApiError as exc:
            logger.error("Slack API error (resolution): %s", exc.response["error"])

    # ═════════════════════════════════════════════════════════
    #  Utility: Thread updates & message edits
    # ═════════════════════════════════════════════════════════

    async def send_thread_update(
        self,
        thread_ts: str,
        message: str,
        channel_id: str | None = None,
    ) -> None:
        """Post a text message in an existing thread."""
        channel = channel_id or self._default_channel
        try:
            await self._client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=message,
            )
        except SlackApiError as exc:
            logger.error("Thread update failed: %s", exc.response["error"])

    async def update_message_processing(
        self,
        channel_id: str | None,
        message_ts: str,
        action_text: str = "Processing...",
    ) -> None:
        """
        Update the original alert message to show a processing state.

        Replaces the action buttons with a "Processing..." indicator.
        """
        channel = channel_id or self._default_channel

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"⏳ *{action_text}*\n_Action triggered — please wait..._",
                },
            },
        ]

        try:
            await self._client.chat_update(
                channel=channel,
                ts=message_ts,
                blocks=blocks,
                text=action_text,
            )
        except SlackApiError as exc:
            logger.error("Message update failed: %s", exc.response["error"])

    async def send_fix_outcome(
        self,
        thread_ts: str,
        success: bool,
        details: str = "",
        channel_id: str | None = None,
    ) -> None:
        """Post a fix outcome message (success or failure)."""
        if success:
            text = f"✅ *Fix applied successfully!*\n{details}"
        else:
            text = f"❌ *Fix failed.*\n{details}\nManual intervention required."

        await self.send_thread_update(thread_ts, text, channel_id)

    async def send_escalation(
        self,
        channel_id: str | None,
        thread_ts: str | None,
        repo: str,
        category: str,
        consecutive_failures: int,
        message: str,
    ) -> None:
        """Post an escalation alert for repeated failures."""
        channel = channel_id or self._default_channel

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🚨 Escalation — Repeated Failures",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{consecutive_failures} consecutive failures* "
                        f"in category `{category}` for `{repo}`.\n\n"
                        f"{message}"
                    ),
                },
            },
        ]

        try:
            await self._client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                blocks=blocks,
                text=f"🚨 Escalation: {consecutive_failures} consecutive {category} failures in {repo}",
            )
        except SlackApiError as exc:
            logger.error("Escalation post failed: %s", exc.response["error"])
