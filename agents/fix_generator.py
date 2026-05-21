"""
Fix Generator Agent — Queries fix history and generates ranked FixProposals.

Responsibilities:
  1. Query PostgreSQL for similar past fixes (by category + error similarity)
  2. Feed history + diagnosis to the LLM to generate 3-5 ranked fix proposals
  3. Apply AUTO_FIX_WHITELIST policy: auto-apply LOW risk fixes for safe categories
  4. Route everything else to Slack for human approval

The whitelist ensures only well-understood, low-risk failure types
are auto-remediated. Everything else gets human eyes first.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic
from sqlalchemy import func, text

from api.models import (
    DiagnosisResult,
    FixProposal,
    PipelineFailureEvent,
    RiskLevel,
    RootCauseCategory,
)
from db.fix_history import FixHistoryRecord, FixOutcome, get_session
from config.settings import get_settings

logger = logging.getLogger("fix_generator")

# ── Auto-fix whitelist ───────────────────────────────────────────
# Only these categories can be auto-applied WITHOUT human approval,
# and ONLY when risk_level == LOW.

AUTO_FIX_WHITELIST: set[str] = {
    RootCauseCategory.FLAKY_TEST.value,
    RootCauseCategory.NETWORK_TIMEOUT.value,
    RootCauseCategory.RESOURCE_EXHAUSTION.value,
}


# ═════════════════════════════════════════════════════════════════
#  History Query Tool
# ═════════════════════════════════════════════════════════════════

def query_fix_history(
    root_cause_category: str,
    error_message: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """
    Query PostgreSQL for the top N most similar past fixes.

    Similarity is ranked by:
      1. Exact category match (highest priority)
      2. Error message substring overlap
      3. Most recent first
      4. Successful fixes ranked above failures

    Args:
        root_cause_category: The diagnosed failure category
        error_message: The error message to find similar matches for
        limit: Max results to return

    Returns:
        List of dicts with: fix_applied, fix_commands, fix_outcome,
        confidence, risk_level, repo, error_message, created_at
    """
    session = get_session()
    try:
        # Build query — prioritise same category + successful outcomes
        query = (
            session.query(FixHistoryRecord)
            .filter(
                FixHistoryRecord.root_cause_category == root_cause_category,
                FixHistoryRecord.fix_applied.isnot(None),
                FixHistoryRecord.fix_applied != "",
            )
            .order_by(
                # Successful fixes first
                (FixHistoryRecord.fix_outcome == FixOutcome.SUCCESS).desc(),
                # Then by recency
                FixHistoryRecord.created_at.desc(),
            )
            .limit(limit)
        )

        records = query.all()

        # If we didn't get enough results, broaden the search
        if len(records) < limit:
            # Search across all categories for similar error messages
            error_keywords = _extract_keywords(error_message)
            if error_keywords:
                broader_query = (
                    session.query(FixHistoryRecord)
                    .filter(
                        FixHistoryRecord.fix_applied.isnot(None),
                        FixHistoryRecord.root_cause_category != root_cause_category,
                    )
                )
                # Add keyword filters
                for keyword in error_keywords[:3]:
                    broader_query = broader_query.filter(
                        FixHistoryRecord.error_message.ilike(f"%{keyword}%")
                    )
                broader_results = (
                    broader_query
                    .order_by(FixHistoryRecord.created_at.desc())
                    .limit(limit - len(records))
                    .all()
                )
                records.extend(broader_results)

        results = []
        for record in records:
            results.append({
                "fix_applied": record.fix_applied,
                "fix_commands": (
                    json.loads(record.fix_commands)
                    if record.fix_commands else []
                ),
                "fix_outcome": record.fix_outcome.value if hasattr(record.fix_outcome, 'value') else str(record.fix_outcome),
                "confidence": record.confidence,
                "risk_level": record.risk_level,
                "repo": record.repo,
                "error_message": (record.error_message or "")[:200],
                "root_cause_category": record.root_cause_category,
                "created_at": record.created_at.isoformat() if record.created_at else None,
            })

        logger.info(
            "Fix history query — category=%s found=%d (limit=%d)",
            root_cause_category, len(results), limit,
        )
        return results

    except Exception as exc:
        logger.error("Fix history query failed: %s", exc)
        return []
    finally:
        session.close()


def _extract_keywords(error_message: str) -> list[str]:
    """Extract meaningful keywords from an error message for fuzzy matching."""
    # Remove common noise words
    noise = {
        "error", "failed", "failure", "the", "a", "an", "is", "was",
        "in", "at", "on", "to", "for", "of", "with", "from", "and",
        "not", "no", "could", "cannot", "unable", "found",
    }
    words = error_message.lower().split()
    keywords = [
        w.strip(".,;:!?\"'()[]{}") for w in words
        if len(w) > 3 and w.lower() not in noise
    ]
    return keywords[:5]


# ═════════════════════════════════════════════════════════════════
#  Fix Generation via LLM
# ═════════════════════════════════════════════════════════════════

FIX_GENERATOR_PROMPT = """\
You are a senior DevOps engineer generating fix proposals for CI/CD pipeline failures.

You will receive:
1. A diagnosis of the pipeline failure (category, error, explanation)
2. Historical fixes for similar failures (with their outcomes)
3. Metadata about the pipeline (repo, branch, workflow)

Generate 3-5 ranked fix proposals. Each fix MUST include:
- description: clear English explanation of what the fix does
- commands: list of specific shell commands OR GitHub Actions workflow changes
- file_patches: dict of filepath → new content (if applicable, else null)
- risk_level: "LOW", "MEDIUM", or "HIGH"
- success_probability: float 0.0-1.0

Ranking rules:
1. Prefer fixes that have worked before (from history)
2. "Retry pipeline" should be first IF the category is flaky_test or network_timeout
3. Lower risk fixes should be ranked higher
4. Be specific — "fix the error" is NOT acceptable

RESPOND ONLY with a valid JSON array of fix proposals. No prose. No markdown.
Example:
[
  {"description": "...", "commands": ["..."], "file_patches": null, "risk_level": "LOW", "success_probability": 0.9},
  {"description": "...", "commands": ["..."], "file_patches": {"path": "content"}, "risk_level": "MEDIUM", "success_probability": 0.7}
]
"""


async def generate_fix_proposals(
    event: PipelineFailureEvent,
    diagnosis: DiagnosisResult,
) -> list[FixProposal]:
    """
    Generate ranked fix proposals using LLM + historical fix data.

    Args:
        event: The pipeline failure event
        diagnosis: The root-cause diagnosis

    Returns:
        List of 3-5 FixProposal objects, ranked by success probability
    """
    settings = get_settings()

    # ── 1. Query fix history ─────────────────────────────────
    history = query_fix_history(
        root_cause_category=diagnosis.root_cause_category.value,
        error_message=diagnosis.error_message,
    )

    logger.info(
        "Generating fixes — category=%s history_matches=%d",
        diagnosis.root_cause_category.value,
        len(history),
    )

    # ── 2. Build LLM prompt ──────────────────────────────────
    user_message = (
        f"## Pipeline Failure Diagnosis\n\n"
        f"- **Repository**: {event.repo_full_name}\n"
        f"- **Branch**: {event.branch}\n"
        f"- **Workflow**: {event.workflow_name}\n"
        f"- **Failed Step**: {diagnosis.failure_step}\n"
        f"- **Category**: {diagnosis.root_cause_category.value}\n"
        f"- **Confidence**: {diagnosis.confidence:.0%}\n\n"
        f"**Error Message**:\n```\n{diagnosis.error_message[:1000]}\n```\n\n"
        f"**Explanation**: {diagnosis.explanation}\n\n"
        f"**Contributing Factors**: {', '.join(diagnosis.contributing_factors)}\n\n"
    )

    if history:
        user_message += "## Historical Fixes for Similar Failures\n\n"
        for i, h in enumerate(history, 1):
            outcome_emoji = "✅" if h["fix_outcome"] == "success" else "❌"
            user_message += (
                f"### Fix #{i} {outcome_emoji}\n"
                f"- **Outcome**: {h['fix_outcome']}\n"
                f"- **Description**: {h['fix_applied']}\n"
                f"- **Commands**: {json.dumps(h['fix_commands'])}\n"
                f"- **Risk**: {h['risk_level']}\n"
                f"- **Error was**: {h['error_message']}\n\n"
            )
    else:
        user_message += "*No historical fixes found for this category.*\n\n"

    user_message += "Generate 3-5 ranked fix proposals as a JSON array."

    # ── 3. Call LLM ──────────────────────────────────────────
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    try:
        response = await client.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=2048,
            system=FIX_GENERATOR_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw_text = response.content[0].text.strip()

        # Parse JSON — handle potential code blocks
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
            raw_text = raw_text.strip()

        proposals_raw = json.loads(raw_text)

    except (json.JSONDecodeError, IndexError, anthropic.APIError) as exc:
        logger.error("Fix generation failed: %s", exc)
        # Return a safe default
        return [_default_retry_proposal(diagnosis)]

    finally:
        await client.close()

    # ── 4. Parse into typed models ───────────────────────────
    proposals: list[FixProposal] = []
    for raw in proposals_raw:
        try:
            risk = RiskLevel(raw.get("risk_level", "HIGH").upper())
            proposal = FixProposal(
                description=raw.get("description", "No description"),
                commands=raw.get("commands", []),
                file_patches=raw.get("file_patches"),
                risk_level=risk,
                success_probability=float(raw.get("success_probability", 0.0)),
            )
            proposals.append(proposal)
        except Exception as exc:
            logger.warning("Skipping malformed proposal: %s", exc)

    # Sort by success probability (highest first)
    proposals.sort(key=lambda p: p.success_probability, reverse=True)

    logger.info("Generated %d fix proposals", len(proposals))
    return proposals if proposals else [_default_retry_proposal(diagnosis)]


# ═════════════════════════════════════════════════════════════════
#  Auto-fix Policy
# ═════════════════════════════════════════════════════════════════

def should_auto_apply(
    diagnosis: DiagnosisResult,
    fix: FixProposal,
) -> bool:
    """
    Determine if a fix should be auto-applied without human approval.

    Auto-apply rules (ALL must be true):
      1. Category is in AUTO_FIX_WHITELIST
      2. Fix risk_level is LOW
      3. Diagnosis confidence >= 0.80
      4. Fix success_probability >= 0.70

    Returns:
        True if the fix can be auto-applied
    """
    category = diagnosis.root_cause_category.value
    is_whitelisted = category in AUTO_FIX_WHITELIST
    is_low_risk = fix.risk_level == RiskLevel.LOW
    is_confident = diagnosis.confidence >= 0.80
    is_likely = fix.success_probability >= 0.70

    decision = is_whitelisted and is_low_risk and is_confident and is_likely

    logger.info(
        "Auto-fix policy — category=%s whitelisted=%s risk=%s "
        "confidence=%.0f%% probability=%.0f%% → %s",
        category, is_whitelisted, fix.risk_level.value,
        diagnosis.confidence * 100, fix.success_probability * 100,
        "AUTO_APPLY" if decision else "NEEDS_APPROVAL",
    )

    return decision


def _default_retry_proposal(diagnosis: DiagnosisResult) -> FixProposal:
    """Fallback: suggest a simple pipeline retry."""
    return FixProposal(
        description="Retry the failed pipeline run (safe default when no specific fix is available)",
        commands=["retry"],
        risk_level=RiskLevel.LOW,
        success_probability=0.3,
    )
