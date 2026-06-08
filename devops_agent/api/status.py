"""
Status & Analytics API — quick health check and operational dashboard.

Endpoints:
  GET /status         → overall agent health + analytics summary
  GET /status/recent  → last N fix history records (default 20)
  GET /status/dlq     → view Dead Letter Queue contents
  POST /status/dlq/replay → replay failed events from DLQ back to main queue
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis
from fastapi import APIRouter, Query

from devops_agent.config.settings import get_settings
from devops_agent.db.fix_history import get_analytics_summary, get_recent_records

logger = logging.getLogger("status_api")

router = APIRouter(prefix="/status", tags=["status"])

# Redis keys
MAIN_QUEUE_KEY = "pipeline_failures"
DLQ_KEY = "pipeline_failures_dlq"


async def _get_redis() -> aioredis.Redis:
    """Get a Redis connection for DLQ operations."""
    settings = get_settings()
    return aioredis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
    )


@router.get("")
async def agent_status():
    """
    Overall agent health check + analytics summary.

    Returns:
      - service info (name, uptime timestamp)
      - analytics: total fixes, success rate, avg duration,
        breakdowns by category and fix method
    """
    try:
        analytics = await get_analytics_summary()
    except Exception as exc:
        logger.error("Analytics query failed: %s", exc)
        analytics = {"error": str(exc)}

    # Include DLQ depth in status
    dlq_depth = 0
    try:
        r = await _get_redis()
        dlq_depth = await r.llen(DLQ_KEY)
        await r.aclose()
    except Exception:
        pass

    return {
        "status": "healthy",
        "service": "devops-pipeline-agent",
        "version": "2.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dlq_depth": dlq_depth,
        "analytics": analytics,
    }


@router.get("/recent")
async def recent_fixes(
    limit: int = Query(default=20, ge=1, le=100, description="Number of records to return"),
):
    """
    Fetch the most recent fix history records.

    Query params:
      - limit: max records to return (1–100, default 20)

    Returns:
      - count: number of records returned
      - records: list of fix history dicts with all analytics fields
    """
    records = await get_recent_records(limit=limit)
    return {
        "count": len(records),
        "records": records,
    }


@router.get("/dlq")
async def view_dlq(
    limit: int = Query(default=20, ge=1, le=50, description="Number of DLQ items to peek"),
):
    """
    View the contents of the Dead Letter Queue without consuming events.

    Returns the most recent failed events that exhausted all retries.
    """
    r = await _get_redis()
    try:
        raw_items = await r.lrange(DLQ_KEY, 0, limit - 1)
        total = await r.llen(DLQ_KEY)

        items = []
        for raw in raw_items:
            try:
                items.append(json.loads(raw))
            except json.JSONDecodeError:
                items.append({"raw": raw})

        return {
            "total_in_dlq": total,
            "showing": len(items),
            "events": items,
        }
    finally:
        await r.aclose()


@router.post("/dlq/replay")
async def replay_dlq(
    count: int = Query(default=1, ge=1, le=10, description="Number of events to replay"),
):
    """
    Replay failed events from the DLQ back to the main processing queue.

    Moves up to `count` events from the DLQ tail back to the main queue.
    """
    r = await _get_redis()
    try:
        replayed = 0
        for _ in range(count):
            event = await r.rpop(DLQ_KEY)
            if event is None:
                break
            await r.lpush(MAIN_QUEUE_KEY, event)
            replayed += 1

        remaining = await r.llen(DLQ_KEY)

        return {
            "replayed": replayed,
            "remaining_in_dlq": remaining,
            "message": f"Replayed {replayed} event(s) back to the main queue",
        }
    finally:
        await r.aclose()
