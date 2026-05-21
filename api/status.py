"""
Status & Analytics API — quick health check and operational dashboard.

Endpoints:
  GET /status         → overall agent health + analytics summary
  GET /status/recent  → last N fix history records (default 20)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query

from db.fix_history import get_analytics_summary, get_recent_records

logger = logging.getLogger("status_api")

router = APIRouter(prefix="/status", tags=["status"])


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
        analytics = get_analytics_summary()
    except Exception as exc:
        logger.error("Analytics query failed: %s", exc)
        analytics = {"error": str(exc)}

    return {
        "status": "healthy",
        "service": "devops-pipeline-agent",
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
    records = get_recent_records(limit=limit)
    return {
        "count": len(records),
        "records": records,
    }
