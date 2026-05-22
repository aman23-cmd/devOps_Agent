"""
GitHub Webhook Receiver — the front door of the DevOps Agent.

Responsibilities:
  1. Accept POST /webhook/github from GitHub
  2. Verify HMAC-SHA256 signature (reject forged payloads)
  3. Filter: only process workflow_run events with conclusion == "failure"
  4. Normalise the payload into a PipelineFailureEvent
  5. Push the JSON event into a Redis list for the agent worker to consume
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis
from fastapi import FastAPI, Header, HTTPException, Request, status

from api.models import PipelineFailureEvent
from config.settings import Settings, get_settings

logger = logging.getLogger("webhook_receiver")


# ── Redis connection pool (created once, shared across requests) ──

_redis_pool: aioredis.Redis | None = None


async def _get_redis(settings: Settings) -> aioredis.Redis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            max_connections=20,
        )
    return _redis_pool


# ── Application lifespan ─────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
    settings = get_settings()
    logging.basicConfig(level=settings.LOG_LEVEL)
    logger.info("Webhook receiver starting — env=%s", settings.ENVIRONMENT)

    # Warm up the Redis connection
    r = await _get_redis(settings)
    await r.ping()
    logger.info("Redis connection verified ✓")

    yield  # ← application is running

    # Graceful shutdown
    if _redis_pool is not None:
        await _redis_pool.aclose()
        logger.info("Redis connection closed")


# ── FastAPI app ──────────────────────────────────────────────────

app = FastAPI(
    title="DevOps Pipeline Agent — Webhook Receiver",
    version="1.0.0",
    lifespan=lifespan,
)

# ── Mount routers ────────────────────────────────────────────────
from api.slack_handler import router as slack_router  # noqa: E402
from api.status import router as status_router  # noqa: E402

app.include_router(slack_router)
app.include_router(status_router)


# ── Signature verification ───────────────────────────────────────


def _verify_signature(payload_body: bytes, secret: str, signature_header: str) -> bool:
    """
    Validate the HMAC-SHA256 digest sent by GitHub.

    GitHub sends: X-Hub-Signature-256: sha256=<hex-digest>
    We recompute the digest locally and compare in constant time.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(
        key=secret.encode("utf-8"),
        msg=payload_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    received = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


# ── Health check ─────────────────────────────────────────────────


@app.get("/health", tags=["ops"])
async def health():
    """Liveness probe for container orchestrators."""
    return {"status": "healthy", "service": "webhook-receiver"}


# ── Webhook endpoint ────────────────────────────────────────────


@app.post(
    "/webhook/github",
    status_code=status.HTTP_200_OK,
    tags=["webhooks"],
    summary="Receive GitHub webhook events",
)
async def receive_github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str | None = Header(None),
):
    """
    Main ingress for GitHub webhook payloads.

    Only `workflow_run` events with `conclusion == "failure"` are
    forwarded to the Redis queue. Everything else is acknowledged
    but silently dropped.
    """
    settings = get_settings()
    raw_body: bytes = await request.body()

    # ── Step 1: Signature verification ───────────────────────
    if not _verify_signature(
        raw_body, settings.GITHUB_WEBHOOK_SECRET, x_hub_signature_256 or ""
    ):
        logger.warning("Invalid webhook signature — rejecting payload")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature",
        )

    # ── Step 2: Parse JSON ───────────────────────────────────
    try:
        payload: dict[str, Any] = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed JSON body",
        )

    # ── Step 3: Event type filtering ─────────────────────────
    event_type = x_github_event or ""
    if event_type != "workflow_run":
        logger.debug("Ignoring event type: %s", event_type)
        return {
            "status": "ok",
            "action": "ignored",
            "reason": f"event_type={event_type}",
        }

    # ── Step 4: Conclusion filtering ─────────────────────────
    action = payload.get("action", "")
    workflow_run: dict[str, Any] = payload.get("workflow_run", {})
    conclusion = workflow_run.get("conclusion", "")

    if action != "completed" or conclusion != "failure":
        logger.debug(
            "Ignoring workflow_run — action=%s conclusion=%s",
            action,
            conclusion,
        )
        return {
            "status": "ok",
            "action": "ignored",
            "reason": f"conclusion={conclusion}",
        }

    # ── Step 5: Build normalised event ───────────────────────
    repo = payload.get("repository", {})
    head_branch = workflow_run.get("head_branch", "unknown")
    head_sha = workflow_run.get("head_sha", "unknown")

    failed_at_raw = workflow_run.get("updated_at")
    try:
        failed_at = datetime.fromisoformat(failed_at_raw.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        failed_at = datetime.now(timezone.utc)

    event = PipelineFailureEvent(
        run_id=workflow_run.get("id", 0),
        repo_full_name=repo.get("full_name", "unknown/unknown"),
        branch=head_branch,
        commit_sha=head_sha,
        workflow_name=workflow_run.get("name", "unknown"),
        failed_at=failed_at,
        html_url=workflow_run.get("html_url"),
    )

    # ── Step 6: Enqueue into Redis ───────────────────────────
    r = await _get_redis(settings)
    await r.lpush(settings.REDIS_QUEUE_KEY, event.model_dump_json())

    queue_length = await r.llen(settings.REDIS_QUEUE_KEY)
    logger.info(
        "Enqueued failure event — run_id=%s repo=%s branch=%s (queue depth: %d)",
        event.run_id,
        event.repo_full_name,
        event.branch,
        queue_length,
    )

    return {
        "status": "ok",
        "action": "enqueued",
        "run_id": event.run_id,
        "repo": event.repo_full_name,
        "queue_depth": queue_length,
    }
