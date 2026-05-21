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
    create_engine,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config.settings import get_settings

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
        Float, nullable=True,
        comment="Total seconds from failure detection to resolution",
    )
    fix_method = Column(
        String(64), nullable=True,
        comment="How the fix was applied: rerun_failed_jobs | pr_created | manual",
    )
    auto_applied = Column(
        Boolean, nullable=False, default=False,
        comment="True if auto-fix whitelist allowed bypass of human approval",
    )
    approved_by = Column(
        String(128), nullable=True,
        comment="Slack username who clicked Apply Fix (null if auto-applied)",
    )
    github_pr_url = Column(
        String(512), nullable=True,
        comment="URL of the PR created by the fix executor",
    )
    pagerduty_incident_id = Column(
        String(64), nullable=True,
        comment="PagerDuty incident ID if escalation was triggered",
    )
    attempt_number = Column(
        Integer, nullable=False, default=1,
        comment="Which attempt this is for the same run_id (1st, 2nd, …)",
    )

    # Slack tracking
    slack_thread_ts = Column(String(64), nullable=True, comment="Slack message timestamp for threading")

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
_SessionLocal = None


def _get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.DATABASE_URL,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            echo=False,
        )
    return _engine


def get_session() -> Session:
    """Return a new SQLAlchemy session."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=_get_engine(), expire_on_commit=False)
    return _SessionLocal()


# ── Bootstrap ───────────────────────────────────────────────────

def create_tables() -> None:
    """
    Create all tables defined by the ORM.

    Safe to call multiple times — SQLAlchemy's create_all()
    is idempotent (CREATE TABLE IF NOT EXISTS).
    """
    engine = _get_engine()
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created / verified ✓")


# ── Analytics queries ───────────────────────────────────────────

def get_recent_records(limit: int = 20) -> list[dict]:
    """
    Fetch the most recent fix history records for the /status endpoint.

    Returns a list of dicts suitable for JSON serialisation.
    """
    session = get_session()
    try:
        records = (
            session.query(FixHistoryRecord)
            .order_by(FixHistoryRecord.created_at.desc())
            .limit(limit)
            .all()
        )
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
                "fix_outcome": r.fix_outcome.value if hasattr(r.fix_outcome, "value") else str(r.fix_outcome),
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
    finally:
        session.close()


def get_analytics_summary() -> dict:
    """
    Return aggregate analytics for the /status endpoint.

    Includes: total fixes, success rate, avg duration,
    breakdown by category and fix method.
    """
    session = get_session()
    try:
        total = session.query(func.count(FixHistoryRecord.id)).scalar() or 0
        successes = (
            session.query(func.count(FixHistoryRecord.id))
            .filter(FixHistoryRecord.fix_outcome == FixOutcome.SUCCESS)
            .scalar() or 0
        )
        failures = (
            session.query(func.count(FixHistoryRecord.id))
            .filter(FixHistoryRecord.fix_outcome == FixOutcome.FAILURE)
            .scalar() or 0
        )
        pending = (
            session.query(func.count(FixHistoryRecord.id))
            .filter(FixHistoryRecord.fix_outcome == FixOutcome.PENDING)
            .scalar() or 0
        )
        avg_duration = (
            session.query(func.avg(FixHistoryRecord.duration_seconds))
            .filter(FixHistoryRecord.duration_seconds.isnot(None))
            .scalar()
        )
        auto_count = (
            session.query(func.count(FixHistoryRecord.id))
            .filter(FixHistoryRecord.auto_applied == True)  # noqa: E712
            .scalar() or 0
        )

        # Category breakdown
        category_rows = (
            session.query(
                FixHistoryRecord.root_cause_category,
                func.count(FixHistoryRecord.id),
            )
            .group_by(FixHistoryRecord.root_cause_category)
            .all()
        )
        by_category = {cat: cnt for cat, cnt in category_rows if cat}

        # Fix method breakdown
        method_rows = (
            session.query(
                FixHistoryRecord.fix_method,
                func.count(FixHistoryRecord.id),
            )
            .group_by(FixHistoryRecord.fix_method)
            .all()
        )
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
    finally:
        session.close()
