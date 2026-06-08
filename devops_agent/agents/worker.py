"""
Worker Process — Consumes pipeline failures from Redis and runs the
full autonomous DevOps Agent workflow.

Pipeline:
  1. BRPOP from Redis "pipeline_failures" queue
  2. Run AutoGen coordinator → diagnosis
  3. Generate fix proposals (LLM + history)
  4. Apply auto-fix policy (whitelist check)
  5. Post Slack alert (TYPE A)
  6. Record in PostgreSQL
  7. If auto-fixable → execute fix → validate outcome
  8. Otherwise → await human approval via Slack buttons

Runs as: python -m agents.worker
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from devops_agent.api.models import (
    DiagnosisResult,
    FixProposal,
    PipelineFailureEvent,
    RiskLevel,
)
from devops_agent.agents.coordinator import run_diagnosis_workflow
from devops_agent.agents.fix_generator import generate_fix_proposals, should_auto_apply
from devops_agent.agents.fix_executor import execute_fix
from devops_agent.agents.slack_notifier import SlackNotifier
from devops_agent.agents.validator import FixValidator
from devops_agent.db.fix_history import FixHistoryRecord, FixOutcome, create_tables, get_session
from sqlalchemy import select
from devops_agent.config.settings import get_settings

logger = logging.getLogger("agent_worker")

# ── Retry configuration ─────────────────────────────────────────
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 5
DLQ_KEY = "pipeline_failures_dlq"


# ── Structured JSON Logging ──────────────────────────────────────


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter for production log aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)


class AgentWorker:
    """
    Production worker bridging:
      Redis → AutoGen → Fix Generator → Slack → Fix Executor → Validator → DB
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._redis: aioredis.Redis | None = None
        self._running = False
        self._notifier = SlackNotifier()

    # ═════════════════════════════════════════════════════════
    #  Lifecycle
    # ═════════════════════════════════════════════════════════

    async def start(self) -> None:
        """Boot the worker and enter the infinite BRPOP loop."""
        # Use structured JSON logging in production, human-readable in dev
        log_format = "%(asctime)s │ %(name)-20s │ %(levelname)-7s │ %(message)s"
        handlers: list[logging.Handler] = []

        if self._settings.ENVIRONMENT == "production":
            handler = logging.StreamHandler()
            handler.setFormatter(JSONFormatter())
            handlers.append(handler)
            logging.basicConfig(
                level=self._settings.LOG_LEVEL,
                handlers=handlers,
            )
        else:
            logging.basicConfig(
                level=self._settings.LOG_LEVEL,
                format=log_format,
                datefmt="%Y-%m-%d %H:%M:%S",
            )

        logger.info("=" * 64)
        logger.info("  DevOps Pipeline Agent — Worker v2")
        logger.info(
            "  env=%s  model=%s",
            self._settings.ENVIRONMENT,
            self._settings.ANTHROPIC_MODEL,
        )
        logger.info(
            "  max_rounds=%d  max_retries=%d",
            self._settings.AUTOGEN_MAX_ROUNDS,
            MAX_RETRIES,
        )
        logger.info("=" * 64)

        await create_tables()

        self._running = True
        
        while self._running:
            try:
                self._redis = aioredis.from_url(
                    self._settings.REDIS_URL,
                    decode_responses=True,
                    max_connections=5,
                )
                await self._redis.ping()
                logger.info("Redis connected ✓")
                break
            except Exception as exc:
                logger.warning("Could not connect to Redis (retrying in 5s): %s", exc)
                await asyncio.sleep(5)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown)
            except NotImplementedError:
                pass

        await self._poll_loop()
        await self._cleanup()

    def _shutdown(self) -> None:
        logger.info("Shutdown signal received — finishing current job...")
        self._running = False

    async def _cleanup(self) -> None:
        if self._redis:
            await self._redis.aclose()
        logger.info("Worker shut down cleanly ✓")

    # ═════════════════════════════════════════════════════════
    #  Main loop
    # ═════════════════════════════════════════════════════════

    async def _poll_loop(self) -> None:
        queue_key = self._settings.REDIS_QUEUE_KEY
        logger.info("Listening on Redis queue: '%s'", queue_key)

        while self._running:
            try:
                if self._redis is None:
                    raise RuntimeError("Redis not connected")
                result = await self._redis.brpop(queue_key, timeout=5)
                if result is None:
                    continue

                _, raw_json = result
                logger.info("━" * 50)
                logger.info("Dequeued pipeline failure event")

                event = PipelineFailureEvent.model_validate_json(raw_json)
                await self._process_with_retries(event)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Poll loop error: %s", exc, exc_info=True)
                await asyncio.sleep(5)

    # ═════════════════════════════════════════════════════════
    #  Retry wrapper (3x, exponential backoff)
    # ═════════════════════════════════════════════════════════

    async def _process_with_retries(self, event: PipelineFailureEvent) -> None:
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(
                    "Attempt %d/%d — run_id=%d repo=%s",
                    attempt,
                    MAX_RETRIES,
                    event.run_id,
                    event.repo_full_name,
                )
                await self._process_event(event)
                return
            except Exception as exc:
                last_error = exc
                backoff = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.error(
                    "Attempt %d/%d failed: %s — retrying in %ds",
                    attempt,
                    MAX_RETRIES,
                    exc,
                    backoff,
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(backoff)

        logger.critical("All %d retries exhausted for run_id=%d", MAX_RETRIES, event.run_id)
        await self._send_failure_alert(event, last_error)

        # Push to Dead Letter Queue so the event is not lost
        try:
            if self._redis is not None:
                await self._redis.lpush(DLQ_KEY, event.model_dump_json())
            logger.info(
                "Event pushed to DLQ — run_id=%d (key=%s)",
                event.run_id,
                DLQ_KEY,
            )
        except Exception as exc:
            logger.error("Failed to push to DLQ: %s", exc)

    # ═════════════════════════════════════════════════════════
    #  Core event processing
    # ═════════════════════════════════════════════════════════

    async def _process_event(self, event: PipelineFailureEvent) -> None:
        """
        Full pipeline:
          1. AutoGen diagnosis
          2. Generate fix proposals (LLM + DB history)
          3. Auto-fix policy check
          4. Slack alert
          5. DB record
          6. Execute + validate (if auto-fixable)
        """
        # ── 1. Diagnose via AutoGen GroupChat ────────────────
        workflow_result = await run_diagnosis_workflow(event)
        diagnosis = DiagnosisResult.model_validate(workflow_result["diagnosis"])

        logger.info(
            "Diagnosis — category=%s confidence=%.0f%%",
            diagnosis.root_cause_category.value,
            diagnosis.confidence * 100,
        )

        # ── 2. Generate fix proposals ────────────────────────
        fix_proposals = await generate_fix_proposals(event, diagnosis)
        best_fix = (
            fix_proposals[0]
            if fix_proposals
            else FixProposal(
                description="No automated fix available — manual investigation required",
                commands=[],
                risk_level=RiskLevel.HIGH,
                success_probability=0.0,
            )
        )

        logger.info(
            "Generated %d fix proposals — best: risk=%s prob=%.0f%%",
            len(fix_proposals),
            best_fix.risk_level.value,
            best_fix.success_probability * 100,
        )

        # ── 3. Auto-fix policy ───────────────────────────────
        auto_apply = should_auto_apply(diagnosis, best_fix)

        # ── 4. Slack alert (TYPE A) ──────────────────────────
        slack_ts = None
        try:
            slack_ts = await self._notifier.send_failure_alert(
                channel_id=None,  # uses default channel
                event=event,
                diagnosis=diagnosis,
                fix_proposals=fix_proposals,
            )
        except Exception as exc:
            logger.error("Slack alert failed: %s", exc)

        # ── 5. Record in PostgreSQL ──────────────────────────
        session_maker = get_session()
        async with session_maker() as session:
            try:
                record = FixHistoryRecord(
                    run_id=event.run_id,
                    repo=event.repo_full_name,
                    branch=event.branch,
                    commit_sha=event.commit_sha,
                    workflow_name=event.workflow_name,
                    error_message=diagnosis.error_message,
                    root_cause_category=diagnosis.root_cause_category.value,
                    confidence=diagnosis.confidence,
                    explanation=diagnosis.explanation,
                    fix_applied=best_fix.description,
                    fix_commands=json.dumps(best_fix.commands),
                    risk_level=best_fix.risk_level.value,
                    fix_outcome=FixOutcome.PENDING,
                    slack_thread_ts=slack_ts,
                )
                session.add(record)
                await session.commit()
                logger.info("Recorded in DB — id=%d", record.id)
            except Exception as exc:
                await session.rollback()
                logger.error("DB write failed: %s", exc)

        # ── 6. Execute + validate (or await approval) ────────
        if diagnosis.action_required == "human_review":
            logger.info("Low confidence — escalated to human review")
            if slack_ts:
                await self._notifier.send_thread_update(
                    thread_ts=slack_ts,
                    message=(
                        f"⚠️ *Low confidence* ({diagnosis.confidence:.0%}) — "
                        f"human review required before any fix is applied."
                    ),
                )

        elif auto_apply:
            logger.info("AUTO-APPLYING fix (whitelisted + LOW risk + high confidence)")
            if slack_ts:
                await self._notifier.send_thread_update(
                    thread_ts=slack_ts,
                    message=(
                        "🤖 *Auto-applying fix* — category is whitelisted, "
                        "risk is LOW, confidence is high."
                    ),
                )
            await self._execute_and_validate(event, best_fix, slack_ts)

        else:
            logger.info("Fix requires human approval via Slack buttons")
            if slack_ts:
                await self._notifier.send_thread_update(
                    thread_ts=slack_ts,
                    message="⏳ Awaiting team approval. Use the buttons above to apply or retry.",
                )

    # ═════════════════════════════════════════════════════════
    #  Execute + Validate
    # ═════════════════════════════════════════════════════════

    async def _execute_and_validate(
        self,
        event: PipelineFailureEvent,
        fix: FixProposal,
        slack_ts: str | None,
    ) -> None:
        """Execute a fix and kick off validation polling."""
        # Execute
        result = await execute_fix(
            fix_proposal=fix,
            run_id=event.run_id,
            repo=event.repo_full_name,
            base_branch=event.branch,
        )

        if result.success:
            if slack_ts:
                details = f"Action: `{result.action_taken}`"
                if result.github_url:
                    details += f"\n<{result.github_url}|View on GitHub>"
                await self._notifier.send_fix_outcome(
                    thread_ts=slack_ts,
                    success=True,
                    details=details,
                )

            # Start validation (polls GitHub for new run)
            validator = FixValidator()
            await validator.validate_fix(
                repo=event.repo_full_name,
                branch=event.branch,
                run_id=event.run_id,
                execution_result=result,
                slack_ts=slack_ts,
            )
        else:
            logger.error("Fix execution failed: %s", result.error)
            if slack_ts:
                await self._notifier.send_fix_outcome(
                    thread_ts=slack_ts,
                    success=False,
                    details=result.error or "Unknown error",
                )

            # Update DB
            session_maker = get_session()
            async with session_maker() as session:
                try:
                    stmt = (
                        select(FixHistoryRecord)
                        .filter_by(run_id=event.run_id)
                        .order_by(FixHistoryRecord.created_at.desc())
                    )
                    result = await session.execute(stmt)
                    record = result.scalars().first()
                    
                    if record:
                        record.fix_outcome = FixOutcome.FAILURE
                        record.resolved_at = datetime.now(timezone.utc)
                        await session.commit()
                except Exception as exc:
                    await session.rollback()
                    logger.error("DB update failed: %s", exc)

    # ═════════════════════════════════════════════════════════
    #  Agent failure alert
    # ═════════════════════════════════════════════════════════

    async def _send_failure_alert(
        self,
        event: PipelineFailureEvent,
        error: Exception | None,
    ) -> None:
        error_msg = str(error) if error else "Unknown error"

        session_maker = get_session()
        async with session_maker() as session:
            try:
                record = FixHistoryRecord(
                    run_id=event.run_id,
                    repo=event.repo_full_name,
                    branch=event.branch,
                    commit_sha=event.commit_sha,
                    workflow_name=event.workflow_name,
                    error_message=f"AGENT FAILURE: {error_msg}",
                    fix_outcome=FixOutcome.FAILURE,
                )
                session.add(record)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.error("Could not record agent failure: %s", exc)

        try:
            from slack_sdk.web.async_client import AsyncWebClient

            client = AsyncWebClient(token=self._settings.SLACK_BOT_TOKEN)
            await client.chat_postMessage(
                channel=self._settings.SLACK_CHANNEL_ID,
                text=(
                    f"🔴 *DevOps Agent failure* — could not process event\n\n"
                    f"*Run ID*: {event.run_id}\n"
                    f"*Repo*: `{event.repo_full_name}`\n"
                    f"*Branch*: `{event.branch}`\n"
                    f"*Error*: ```{error_msg[:500]}```\n\n"
                    f"All {MAX_RETRIES} attempts exhausted. Manual intervention required."
                ),
            )
        except Exception as exc:
            logger.error("Could not send failure alert: %s", exc)


# ── Entry point ──────────────────────────────────────────────────


async def main() -> None:
    worker = AgentWorker()
    await worker.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Worker interrupted — exiting")
        sys.exit(0)
