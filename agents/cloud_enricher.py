"""
Cloud Log Enricher Agent — Gathers infrastructure context from cloud providers.

This agent queries external logging systems to provide infrastructure
context that CI/CD logs alone cannot reveal:
  • Database connection errors
  • Memory pressure / OOMKill events
  • Network anomalies
  • Infrastructure-level failures

Supports:
  1. GCP Cloud Logging (if GCP_PROJECT_ID is set)
  2. AWS CloudWatch Logs (if AWS_REGION + AWS_LOG_GROUP are set)
  3. Graceful no-op if neither is configured

Registered as a tool on a UserProxyAgent in the AutoGen GroupChat.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from config.settings import get_settings

logger = logging.getLogger("cloud_enricher_agent")

# ── Time window for cloud log queries ────────────────────────────
WINDOW_MINUTES = 15


async def get_cloud_context(
    repo: str,
    failed_at_timestamp: str,
) -> dict[str, Any]:
    """
    Query cloud logging APIs for infrastructure context around the failure.

    This function is registered as a tool on the Cloud Enricher
    UserProxyAgent within the AutoGen GroupChat.

    Args:
        repo: Full repository name (e.g. "owner/repo") — used for log filtering
        failed_at_timestamp: ISO-8601 timestamp of the pipeline failure

    Returns:
        {
            "provider": str,           # "gcp" | "aws" | "none"
            "db_errors": list[str],
            "memory_events": list[str],
            "network_anomalies": list[str],
            "infrastructure_summary": str,
            "raw_entries_count": int,
        }
    """
    settings = get_settings()

    # Parse the failure timestamp
    try:
        failed_at = datetime.fromisoformat(failed_at_timestamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        failed_at = datetime.now(timezone.utc)

    window_start = failed_at - timedelta(minutes=WINDOW_MINUTES)
    window_end = failed_at + timedelta(minutes=WINDOW_MINUTES)

    # ── Try GCP first ────────────────────────────────────────
    if settings.GCP_PROJECT_ID:
        logger.info("Querying GCP Cloud Logging — project=%s", settings.GCP_PROJECT_ID)
        try:
            return await _query_gcp_logs(
                project_id=settings.GCP_PROJECT_ID,
                repo=repo,
                start=window_start,
                end=window_end,
            )
        except Exception as exc:
            logger.error("GCP Cloud Logging query failed: %s", exc)

    # ── Fall back to AWS ─────────────────────────────────────
    if settings.AWS_REGION and settings.AWS_LOG_GROUP:
        logger.info(
            "Querying AWS CloudWatch — region=%s group=%s",
            settings.AWS_REGION,
            settings.AWS_LOG_GROUP,
        )
        try:
            return await _query_aws_cloudwatch(
                region=settings.AWS_REGION,
                log_group=settings.AWS_LOG_GROUP,
                repo=repo,
                start=window_start,
                end=window_end,
            )
        except Exception as exc:
            logger.error("AWS CloudWatch query failed: %s", exc)

    # ── No cloud provider configured ─────────────────────────
    logger.info("No cloud provider configured — skipping enrichment")
    return {
        "provider": "none",
        "db_errors": [],
        "memory_events": [],
        "network_anomalies": [],
        "infrastructure_summary": "No cloud logging configured. Diagnosis relies on CI/CD logs only.",
        "raw_entries_count": 0,
    }


# ═════════════════════════════════════════════════════════════════
#  GCP Cloud Logging
# ═════════════════════════════════════════════════════════════════

async def _query_gcp_logs(
    project_id: str,
    repo: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """
    Query GCP Cloud Logging for ERROR and CRITICAL entries.

    Uses the google-cloud-logging client library.
    Requires GOOGLE_APPLICATION_CREDENTIALS env var or
    workload identity on GKE.
    """
    from google.cloud import logging as gcp_logging

    client = gcp_logging.Client(project=project_id)

    # Build the filter
    # Scope to ERROR/CRITICAL severity in the time window
    service_name = repo.split("/")[-1] if "/" in repo else repo
    log_filter = (
        f'severity >= "ERROR" '
        f'AND timestamp >= "{start.isoformat()}" '
        f'AND timestamp <= "{end.isoformat()}"'
    )

    logger.debug("GCP filter: %s", log_filter)

    db_errors: list[str] = []
    memory_events: list[str] = []
    network_anomalies: list[str] = []
    raw_count = 0

    # Execute query (synchronous client, run in executor if needed)
    entries = client.list_entries(
        filter_=log_filter,
        max_results=200,
        order_by="timestamp desc",
    )

    for entry in entries:
        raw_count += 1
        message = str(entry.payload) if entry.payload else ""
        severity = entry.severity or "UNKNOWN"

        # Classify the log entry
        msg_lower = message.lower()

        if any(kw in msg_lower for kw in [
            "connection refused", "connection reset", "database",
            "postgresql", "mysql", "mongodb", "redis timeout",
            "sqlalchemy", "deadlock", "lock timeout",
        ]):
            db_errors.append(f"[{severity}] {message[:300]}")

        elif any(kw in msg_lower for kw in [
            "oom", "out of memory", "memory", "oomkilled",
            "killed process", "memory pressure", "heap",
            "gc overhead", "allocation failed",
        ]):
            memory_events.append(f"[{severity}] {message[:300]}")

        elif any(kw in msg_lower for kw in [
            "network", "dns", "timeout", "connection timed out",
            "unreachable", "socket", "ssl", "certificate",
            "502", "503", "504", "gateway",
        ]):
            network_anomalies.append(f"[{severity}] {message[:300]}")

    # Build summary
    parts: list[str] = []
    if db_errors:
        parts.append(f"{len(db_errors)} database errors")
    if memory_events:
        parts.append(f"{len(memory_events)} memory events")
    if network_anomalies:
        parts.append(f"{len(network_anomalies)} network anomalies")

    summary = (
        f"GCP Cloud Logging: {raw_count} ERROR/CRITICAL entries found. "
        + (", ".join(parts) if parts else "No categorised issues detected.")
    )

    return {
        "provider": "gcp",
        "db_errors": db_errors[:10],
        "memory_events": memory_events[:10],
        "network_anomalies": network_anomalies[:10],
        "infrastructure_summary": summary,
        "raw_entries_count": raw_count,
    }


# ═════════════════════════════════════════════════════════════════
#  AWS CloudWatch
# ═════════════════════════════════════════════════════════════════

async def _query_aws_cloudwatch(
    region: str,
    log_group: str,
    repo: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """
    Query AWS CloudWatch Logs using CloudWatch Logs Insights.

    Uses boto3. Requires AWS credentials via env vars,
    IAM role, or ~/.aws/credentials.
    """
    import boto3

    client = boto3.client("logs", region_name=region)

    # CloudWatch Logs Insights query
    query = (
        "fields @timestamp, @message, @logStream "
        "| filter @message like /(?i)(error|fatal|exception|oom|timeout|refused)/ "
        f"| sort @timestamp desc "
        "| limit 200"
    )

    start_epoch = int(start.timestamp())
    end_epoch = int(end.timestamp())

    response = client.start_query(
        logGroupName=log_group,
        startTime=start_epoch,
        endTime=end_epoch,
        queryString=query,
    )

    query_id = response["queryId"]

    # Poll for results (CloudWatch Insights is async)
    import asyncio
    results = None
    for _ in range(30):  # max 30 seconds
        result_response = client.get_query_results(queryId=query_id)
        if result_response["status"] == "Complete":
            results = result_response.get("results", [])
            break
        await asyncio.sleep(1)

    if results is None:
        results = []

    db_errors: list[str] = []
    memory_events: list[str] = []
    network_anomalies: list[str] = []

    for row in results:
        # Each row is a list of {field, value} dicts
        message = ""
        for field in row:
            if field.get("field") == "@message":
                message = field.get("value", "")
                break

        msg_lower = message.lower()

        if any(kw in msg_lower for kw in [
            "database", "rds", "connection refused", "deadlock",
            "postgresql", "mysql", "dynamodb",
        ]):
            db_errors.append(message[:300])

        elif any(kw in msg_lower for kw in [
            "oom", "out of memory", "memory", "killed",
        ]):
            memory_events.append(message[:300])

        elif any(kw in msg_lower for kw in [
            "timeout", "network", "unreachable", "dns",
            "502", "503", "504",
        ]):
            network_anomalies.append(message[:300])

    parts: list[str] = []
    if db_errors:
        parts.append(f"{len(db_errors)} database errors")
    if memory_events:
        parts.append(f"{len(memory_events)} memory events")
    if network_anomalies:
        parts.append(f"{len(network_anomalies)} network anomalies")

    summary = (
        f"AWS CloudWatch: {len(results)} log entries found. "
        + (", ".join(parts) if parts else "No categorised issues detected.")
    )

    return {
        "provider": "aws",
        "db_errors": db_errors[:10],
        "memory_events": memory_events[:10],
        "network_anomalies": network_anomalies[:10],
        "infrastructure_summary": summary,
        "raw_entries_count": len(results),
    }
