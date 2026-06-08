"""
Fix History — SQLAlchemy 2.0 model that records every diagnosis
and remediation attempt the agent makes.

This table gives you:
  • An audit trail for every automated fix
  • Data for feedback loops (which fixes actually work?)
  • Root-cause analytics over time
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    Integer,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from devops_agent.config.settings import get_settings

logger = logging.getLogger("fix_history")


# ── Enums ────────────────────────────────────────────────────────


class FixOutcome(str, PyEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    PENDING = "pending"
    SKIPPED = "skipped"


# ── SQLAlchemy 2.0 declarative base ─────────────────────────────


class Base(DeclarativeBase):
    pass


class FixHistoryRecord(Base):
    """
    One row per pipeline failure that the agent processed.
    """

    __tablename__ = "fix_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, nullable=False, index=True, comment="GitHub Actions run ID")
    repo = Column(String(256), nullable=False, index=True)
    branch = Column(String(256), nullable=True)
    commit_sha = Column(String(40), nullable=True)
    workflow_name = Column(String(256), nullable=True)

    # Diagnosis
    error_message = Column(Text, nullable=True)
    root_cause_category = Column(String(64), nullable=True, index=True)
    confidence = Column(Float, nullable=True)
    explanation = Column(Text, nullable=True)

    # Fix
    fix_applied = Column(Text, nullable=True, comment="Description of the fix that was applied")
    fix_commands = Column(Text, nullable=True, comment="JSON array of commands executed")
    risk_level = Column(String(16), nullable=True)

    # Outcome
    fix_outcome = Column(
        Enum(FixOutcome, name="fix_outcome_enum"),
        nullable=False,
        default=FixOutcome.PENDING,
    )

    # ── Analytics columns ────────────────────────────────────
    duration_seconds = Column(
        Float,
        nullable=True,
        comment="Total seconds from failure detection to resolution",
    )
    fix_method = Column(
        String(64),
        nullable=True,
        comment="How the fix was applied: rerun_failed_jobs | pr_created | manual",
    )
    auto_applied = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="True if auto-fix whitelist allowed bypass of human approval",
    )
    approved_by = Column(
        String(128),
        nullable=True,
        comment="Slack username who clicked Apply Fix (null if auto-applied)",
    )
    github_pr_url = Column(
        String(512),
        nullable=True,
        comment="URL of the PR created by the fix executor",
    )
    pagerduty_incident_id = Column(
        String(64),
        nullable=True,
        comment="PagerDuty incident ID if escalation was triggered",
    )
    attempt_number = Column(
        Integer,
        nullable=False,
        default=1,
        comment="Which attempt this is for the same run_id (1st, 2nd, …)",
    )

    # Slack tracking
    slack_thread_ts = Column(
        String(64), nullable=True, comment="Slack message timestamp for threading"
    )

    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    # ── Helpers ──────────────────────────────────────────────

    @property
    def formatted_duration(self) -> str:
        """Human-readable duration string."""
        if self.duration_seconds is None:
            return "N/A"
        s = self.duration_seconds
        if s < 60:
            return f"{s:.0f}s"
        elif s < 3600:
            return f"{int(s // 60)}m {int(s % 60)}s"
        else:
            return f"{int(s // 3600)}h {int((s % 3600) // 60)}m"

    def __repr__(self) -> str:
        return (
            f"<FixHistoryRecord id={self.id} run_id={self.run_id} "
            f"repo={self.repo} outcome={self.fix_outcome}>"
        )


# ── Engine & session factory ────────────────────────────────────

_engine = None
_AsyncSessionLocal = None


def _get_engine():
    """Create or return the cached async SQLAlchemy engine.

    Applies dialect-specific configuration:
      - SQLite: ``check_same_thread=False`` via *connect_args*
      - PostgreSQL: ``pool_pre_ping=True`` for connection health checks
    """
    global _engine
    if _engine is None:
        settings = get_settings()
        is_sqlite = settings.DATABASE_URL.startswith("sqlite")

        connect_args: dict[str, object] = {}
        engine_kwargs: dict[str, object] = {"echo": False}

        if is_sqlite:
            connect_args["check_same_thread"] = False
        else:
            # pool_pre_ping only makes sense for pooled connections (PostgreSQL)
            engine_kwargs["pool_pre_ping"] = True

        _engine = create_async_engine(
            settings.DATABASE_URL,
            connect_args=connect_args,
            **engine_kwargs,
        )
    return _engine


def get_session() -> async_sessionmaker[AsyncSession]:
    """Return an async session factory."""
    global _AsyncSessionLocal
    if _AsyncSessionLocal is None:
        _AsyncSessionLocal = async_sessionmaker(bind=_get_engine(), expire_on_commit=False)
    return _AsyncSessionLocal


# ── Bootstrap ───────────────────────────────────────────────────


async def create_tables() -> None:
    """
    Create all tables defined by the ORM.

    Safe to call multiple times — SQLAlchemy's create_all()
    is idempotent (CREATE TABLE IF NOT EXISTS).
    Only runs automatically for SQLite.
    """
    settings = get_settings()
    db_type = "SQLite (dev mode)" if settings.DATABASE_URL.startswith("sqlite") else "PostgreSQL"
    logger.info("Database: %s", db_type)

    if settings.DATABASE_URL.startswith("sqlite"):
        engine = _get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables created / verified ✓")


# ── Analytics queries ───────────────────────────────────────────


async def get_recent_records(limit: int = 20) -> list[dict]:
    """
    Fetch the most recent fix history records for the /status endpoint.

    Returns a list of dicts suitable for JSON serialisation.
    """
    session_maker = get_session()
    async with session_maker() as session:
        try:
            stmt = (
                select(FixHistoryRecord).order_by(FixHistoryRecord.created_at.desc()).limit(limit)
            )
            result = await session.execute(stmt)
            records = result.scalars().all()

            return [
                {
                    "id": r.id,
                    "run_id": r.run_id,
                    "repo": r.repo,
                    "branch": r.branch,
                    "workflow_name": r.workflow_name,
                    "root_cause_category": r.root_cause_category,
                    "confidence": r.confidence,
                    "fix_applied": r.fix_applied,
                    "fix_outcome": (
                        r.fix_outcome.value
                        if hasattr(r.fix_outcome, "value")
                        else str(r.fix_outcome)
                    ),
                    "risk_level": r.risk_level,
                    "fix_method": r.fix_method,
                    "auto_applied": r.auto_applied,
                    "approved_by": r.approved_by,
                    "github_pr_url": r.github_pr_url,
                    "duration_seconds": r.duration_seconds,
                    "formatted_duration": r.formatted_duration,
                    "attempt_number": r.attempt_number,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
                }
                for r in records
            ]
        except Exception as exc:
            logger.error("get_recent_records failed: %s", exc)
            return []


async def get_analytics_summary() -> dict:
    """
    Return aggregate analytics for the /status endpoint.

    Includes: total fixes, success rate, avg duration,
    breakdown by category and fix method.
    """
    session_maker = get_session()
    async with session_maker() as session:
        try:
            total = (await session.execute(select(func.count(FixHistoryRecord.id)))).scalar() or 0

            stmt_succ = select(func.count(FixHistoryRecord.id)).where(
                FixHistoryRecord.fix_outcome == FixOutcome.SUCCESS
            )
            successes = (await session.execute(stmt_succ)).scalar() or 0

            stmt_fail = select(func.count(FixHistoryRecord.id)).where(
                FixHistoryRecord.fix_outcome == FixOutcome.FAILURE
            )
            failures = (await session.execute(stmt_fail)).scalar() or 0

            stmt_pend = select(func.count(FixHistoryRecord.id)).where(
                FixHistoryRecord.fix_outcome == FixOutcome.PENDING
            )
            pending = (await session.execute(stmt_pend)).scalar() or 0

            stmt_dur = select(func.avg(FixHistoryRecord.duration_seconds)).where(
                FixHistoryRecord.duration_seconds.isnot(None)
            )
            avg_duration = (await session.execute(stmt_dur)).scalar()

            stmt_auto = select(func.count(FixHistoryRecord.id)).where(
                FixHistoryRecord.auto_applied.is_(True)
            )
            auto_count = (await session.execute(stmt_auto)).scalar() or 0

            # Category breakdown
            stmt_cat = select(
                FixHistoryRecord.root_cause_category, func.count(FixHistoryRecord.id)
            ).group_by(FixHistoryRecord.root_cause_category)
            category_rows = (await session.execute(stmt_cat)).all()
            by_category = {cat: cnt for cat, cnt in category_rows if cat}

            # Fix method breakdown
            stmt_method = select(
                FixHistoryRecord.fix_method, func.count(FixHistoryRecord.id)
            ).group_by(FixHistoryRecord.fix_method)
            method_rows = (await session.execute(stmt_method)).all()
            by_method = {m: cnt for m, cnt in method_rows if m}

            return {
                "total_fixes": total,
                "successes": successes,
                "failures": failures,
                "pending": pending,
                "success_rate": round(successes / total, 3) if total > 0 else 0.0,
                "avg_duration_seconds": round(avg_duration, 1) if avg_duration else None,
                "auto_applied_count": auto_count,
                "by_category": by_category,
                "by_fix_method": by_method,
            }
        except Exception as exc:
            logger.error("get_analytics_summary failed: %s", exc)
            return {"error": str(exc)}
