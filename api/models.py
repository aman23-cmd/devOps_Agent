"""
Pydantic v2 schemas used across the entire pipeline.

Three core models:
  • PipelineFailureEvent  – what the webhook receiver enqueues into Redis
  • DiagnosisResult        – what the LLM returns after log analysis
  • FixProposal            – the actionable remediation suggestion
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


# ── Enums ────────────────────────────────────────────────────────

class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RootCauseCategory(str, Enum):
    """Granular failure classifications used by the diagnosis agent."""
    FLAKY_TEST = "flaky_test"
    DEPENDENCY_ISSUE = "dependency_issue"
    ENV_MISMATCH = "env_mismatch"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    NETWORK_TIMEOUT = "network_timeout"
    CODE_REGRESSION = "code_regression"
    CONFIG_ERROR = "config_error"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    UNKNOWN = "unknown"


# ── Pipeline Failure (webhook → Redis) ───────────────────────────

class PipelineFailureEvent(BaseModel):
    """Normalised payload pushed to the Redis queue."""
    run_id: int = Field(..., description="GitHub Actions workflow run ID")
    repo_full_name: str = Field(..., description="owner/repo")
    branch: str = Field(..., description="Branch that triggered the run")
    commit_sha: str = Field(..., description="HEAD commit SHA")
    workflow_name: str = Field(..., description="Name of the workflow file")
    failed_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="UTC timestamp of the failure",
    )
    html_url: Optional[str] = Field(
        None,
        description="Direct link to the failed run on GitHub",
    )

    model_config = {"json_encoders": {datetime: lambda v: v.isoformat()}}


# ── Diagnosis (LLM output) ──────────────────────────────────────

class DiagnosisResult(BaseModel):
    """Structured root-cause analysis returned by the AI agent."""
    failure_step: str = Field(
        ...,
        description="CI step name where the failure originated (e.g. 'npm install')",
    )
    error_message: str = Field(
        ...,
        description="Key error string extracted from the logs",
    )
    root_cause_category: RootCauseCategory = Field(
        ...,
        description="High-level failure classification",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Model's self-assessed confidence (0-1)",
    )
    explanation: str = Field(
        ...,
        description="Human-readable explanation of what went wrong and why",
    )
    contributing_factors: List[str] = Field(
        default_factory=list,
        description="Secondary issues that may have contributed to the failure",
    )
    action_required: Optional[str] = Field(
        None,
        description="Set to 'human_review' when confidence < 0.80",
    )


# ── Fix Proposal ────────────────────────────────────────────────

class FixProposal(BaseModel):
    """Actionable remediation the agent can apply or suggest."""
    description: str = Field(
        ...,
        description="Plain-English summary of the proposed fix",
    )
    commands: List[str] = Field(
        default_factory=list,
        description="Shell commands or code changes to execute",
    )
    file_patches: Optional[dict] = Field(
        None,
        description="Map of filepath → unified diff to apply",
    )
    risk_level: RiskLevel = Field(
        ...,
        description="Assessed risk of applying this fix",
    )
    success_probability: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Estimated probability the fix resolves the issue",
    )
