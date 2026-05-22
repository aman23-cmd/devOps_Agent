"""
Coordinator Agent — Orchestrates the multi-agent diagnosis workflow.

Uses Microsoft AutoGen GroupChat to run a structured conversation:
  1. Log Fetcher Agent   → downloads and parses GitHub CI/CD logs
  2. Cloud Enricher Agent → queries GCP/AWS for infrastructure context
  3. Diagnosis Agent      → analyses everything and produces structured diagnosis

Architecture:
  - Log Fetcher: UserProxyAgent with `fetch_github_logs` tool
  - Cloud Enricher: UserProxyAgent with `get_cloud_context` tool
  - Diagnosis: AssistantAgent with engineered system prompt
  - Coordinator: UserProxyAgent that initiates and manages the flow
  - GroupChat + GroupChatManager ties them together

The coordinator sends the initial message containing the failure event,
the agents collaborate, and the final diagnosis JSON is extracted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import autogen

from api.models import (
    DiagnosisResult,
    FixProposal,
    PipelineFailureEvent,
    RiskLevel,
    RootCauseCategory,
)
from agents.diagnosis import get_diagnosis_system_prompt
from agents.log_fetcher import fetch_github_logs
from agents.cloud_enricher import get_cloud_context
from config.settings import get_settings

logger = logging.getLogger("coordinator")


# ── LLM config builder ──────────────────────────────────────────


def _build_llm_config() -> dict[str, Any]:
    """
    Build the AutoGen llm_config for Claude claude-sonnet-4-20250514 via Anthropic API.

    AutoGen 0.2.x supports Anthropic models through the
    api_type="anthropic" configuration.
    """
    settings = get_settings()

    return {
        "config_list": [
            {
                "model": settings.ANTHROPIC_MODEL,
                "api_key": settings.ANTHROPIC_API_KEY,
                "api_type": "anthropic",
            }
        ],
        "temperature": 0.1,  # low temp for deterministic diagnosis
        "cache_seed": None,  # disable caching for fresh analysis
    }


# ── Synchronous tool wrappers ────────────────────────────────────
# AutoGen 0.2.x tool registration expects synchronous functions.
# We wrap our async tools so they can be called from within the
# GroupChat execution context.


def _sync_fetch_logs(run_id: int, repo: str) -> str:
    """Synchronous wrapper around the async log fetcher."""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        # We're inside an async context — use nest_asyncio pattern
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(
                asyncio.run, fetch_github_logs(run_id=run_id, repo=repo)
            ).result(timeout=120)
    else:
        result = asyncio.run(fetch_github_logs(run_id=run_id, repo=repo))

    return json.dumps(result, indent=2, default=str)


def _sync_get_cloud_context(repo: str, failed_at_timestamp: str) -> str:
    """Synchronous wrapper around the async cloud enricher."""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(
                asyncio.run,
                get_cloud_context(repo=repo, failed_at_timestamp=failed_at_timestamp),
            ).result(timeout=60)
    else:
        result = asyncio.run(
            get_cloud_context(repo=repo, failed_at_timestamp=failed_at_timestamp)
        )

    return json.dumps(result, indent=2, default=str)


# ── Agent factory ────────────────────────────────────────────────


def _create_agents(
    llm_config: dict[str, Any],
) -> tuple[
    autogen.UserProxyAgent,
    autogen.UserProxyAgent,
    autogen.UserProxyAgent,
    autogen.AssistantAgent,
]:
    """
    Create the four agents that participate in the GroupChat.

    Returns:
        (coordinator, log_fetcher, cloud_enricher, diagnosis_agent)
    """

    # ── 1. Coordinator (initiator) ───────────────────────────
    coordinator = autogen.UserProxyAgent(
        name="Coordinator",
        system_message=(
            "You are the DevOps Pipeline Agent coordinator. "
            "You initiate the diagnosis workflow by providing the failure event. "
            "You orchestrate the conversation flow:\n"
            "1. First ask LogFetcher to fetch the CI/CD logs\n"
            "2. Then ask CloudEnricher to get infrastructure context\n"
            "3. Then ask DiagnosisAgent to analyse everything and produce a diagnosis\n"
            "4. Once you receive a valid JSON diagnosis, say TERMINATE\n\n"
            "Do NOT diagnose anything yourself. Your job is ONLY to coordinate."
        ),
        human_input_mode="NEVER",
        max_consecutive_auto_reply=10,
        code_execution_config=False,
        llm_config=llm_config,
    )

    # ── 2. Log Fetcher Agent ─────────────────────────────────
    log_fetcher = autogen.UserProxyAgent(
        name="LogFetcher",
        system_message=(
            "You are the Log Fetcher agent. When asked, you fetch CI/CD logs "
            "from GitHub Actions using the fetch_github_logs tool. "
            "Always call the tool with the run_id and repo provided by the Coordinator. "
            "Return the tool output exactly as received — do not summarise or modify it."
        ),
        human_input_mode="NEVER",
        max_consecutive_auto_reply=3,
        code_execution_config=False,
        llm_config=llm_config,
    )

    # Register the log fetcher tool
    autogen.register_function(
        _sync_fetch_logs,
        caller=log_fetcher,
        executor=log_fetcher,
        name="fetch_github_logs",
        description=(
            "Fetch CI/CD workflow logs from GitHub Actions. "
            "Parameters: run_id (int), repo (str like 'owner/repo'). "
            "Returns JSON with: failed_step, error_message, truncated_logs, stack_trace."
        ),
    )

    # ── 3. Cloud Enricher Agent ──────────────────────────────
    cloud_enricher = autogen.UserProxyAgent(
        name="CloudEnricher",
        system_message=(
            "You are the Cloud Log Enricher agent. When asked, you query cloud "
            "logging systems (GCP Cloud Logging or AWS CloudWatch) for infrastructure "
            "context around the failure timestamp. "
            "Call the get_cloud_context tool with the repo and failure timestamp. "
            "Return the tool output exactly as received."
        ),
        human_input_mode="NEVER",
        max_consecutive_auto_reply=3,
        code_execution_config=False,
        llm_config=llm_config,
    )

    # Register the cloud enricher tool
    autogen.register_function(
        _sync_get_cloud_context,
        caller=cloud_enricher,
        executor=cloud_enricher,
        name="get_cloud_context",
        description=(
            "Query cloud logging APIs for infrastructure context. "
            "Parameters: repo (str), failed_at_timestamp (ISO-8601 string). "
            "Returns JSON with: db_errors, memory_events, network_anomalies, infrastructure_summary."
        ),
    )

    # ── 4. Diagnosis Agent ───────────────────────────────────
    diagnosis_agent = autogen.AssistantAgent(
        name="DiagnosisAgent",
        system_message=get_diagnosis_system_prompt(),
        llm_config=llm_config,
        human_input_mode="NEVER",
        max_consecutive_auto_reply=2,
    )

    return coordinator, log_fetcher, cloud_enricher, diagnosis_agent


# ── GroupChat setup ──────────────────────────────────────────────


def _create_group_chat(
    coordinator: autogen.UserProxyAgent,
    log_fetcher: autogen.UserProxyAgent,
    cloud_enricher: autogen.UserProxyAgent,
    diagnosis_agent: autogen.AssistantAgent,
    max_rounds: int,
) -> tuple[autogen.GroupChat, autogen.GroupChatManager]:
    """
    Create the GroupChat and GroupChatManager.

    Speaker selection follows the workflow order:
      Coordinator → LogFetcher → CloudEnricher → DiagnosisAgent → Coordinator (TERMINATE)
    """
    group_chat = autogen.GroupChat(
        agents=[coordinator, log_fetcher, cloud_enricher, diagnosis_agent],
        messages=[],
        max_round=max_rounds,
        speaker_selection_method="auto",
        allow_repeat_speaker=False,
    )

    llm_config = _build_llm_config()

    manager = autogen.GroupChatManager(
        groupchat=group_chat,
        llm_config=llm_config,
    )

    return group_chat, manager


# ── Result extraction ────────────────────────────────────────────


def _extract_diagnosis_from_chat(
    chat_history: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """
    Parse the GroupChat message history to find the diagnosis JSON.

    The DiagnosisAgent should output raw JSON. We scan messages
    in reverse to find the last valid JSON block containing
    the expected "diagnosis" key.
    """
    for message in reversed(chat_history):
        content = message.get("content", "")
        if not content or not isinstance(content, str):
            continue

        # Try to parse the entire message as JSON
        parsed = _try_parse_json(content)
        if parsed and "diagnosis" in parsed:
            return parsed

        # Try to extract JSON from within the message (code blocks etc.)
        json_blocks = re.findall(
            r"```(?:json)?\s*\n?(.*?)\n?\s*```",
            content,
            re.DOTALL,
        )
        for block in json_blocks:
            parsed = _try_parse_json(block)
            if parsed and "diagnosis" in parsed:
                return parsed

        # Try to find raw JSON object in the text
        brace_match = re.search(r"\{.*\}", content, re.DOTALL)
        if brace_match:
            parsed = _try_parse_json(brace_match.group())
            if parsed and "diagnosis" in parsed:
                return parsed

    return None


def _try_parse_json(text: str) -> dict[str, Any] | None:
    """Attempt to parse a string as JSON, returning None on failure."""
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        return None


# ── Main workflow function ───────────────────────────────────────


async def run_diagnosis_workflow(
    event: PipelineFailureEvent,
) -> dict[str, Any]:
    """
    Execute the full multi-agent diagnosis workflow for a pipeline failure.

    This is the main entry point called by the worker process.

    Args:
        event: The normalised pipeline failure event from Redis

    Returns:
        {
            "diagnosis": DiagnosisResult (as dict),
            "fix_proposals": list[FixProposal] (as dicts),
            "action_taken": str,
            "raw_chat_history": list[dict],
        }
    """
    settings = get_settings()
    llm_config = _build_llm_config()

    logger.info(
        "Starting diagnosis workflow — run_id=%d repo=%s branch=%s",
        event.run_id,
        event.repo_full_name,
        event.branch,
    )

    # ── Create agents ────────────────────────────────────────
    coordinator, log_fetcher, cloud_enricher, diagnosis_agent = _create_agents(
        llm_config
    )

    # ── Create GroupChat ─────────────────────────────────────
    group_chat, manager = _create_group_chat(
        coordinator=coordinator,
        log_fetcher=log_fetcher,
        cloud_enricher=cloud_enricher,
        diagnosis_agent=diagnosis_agent,
        max_rounds=settings.AUTOGEN_MAX_ROUNDS,
    )

    # ── Build the initial message ────────────────────────────
    initial_message = (
        f"## New Pipeline Failure Detected\n\n"
        f"**Run ID**: {event.run_id}\n"
        f"**Repository**: {event.repo_full_name}\n"
        f"**Branch**: {event.branch}\n"
        f"**Commit**: {event.commit_sha}\n"
        f"**Workflow**: {event.workflow_name}\n"
        f"**Failed At**: {event.failed_at.isoformat()}\n"
        f"**URL**: {event.html_url or 'N/A'}\n\n"
        f"Please begin the diagnosis workflow:\n"
        f"1. LogFetcher: fetch logs for run_id={event.run_id}, repo='{event.repo_full_name}'\n"
        f"2. CloudEnricher: get cloud context for repo='{event.repo_full_name}', "
        f"failed_at='{event.failed_at.isoformat()}'\n"
        f"3. DiagnosisAgent: analyse all collected data and produce a JSON diagnosis\n"
    )

    # ── Run the GroupChat ────────────────────────────────────
    logger.info("Initiating GroupChat with %d agents", len(group_chat.agents))

    # Run in a thread to avoid blocking the async event loop
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: coordinator.initiate_chat(
            manager,
            message=initial_message,
        ),
    )

    # ── Extract diagnosis from chat history ──────────────────
    chat_messages = group_chat.messages
    logger.info("GroupChat completed — %d messages exchanged", len(chat_messages))

    raw_diagnosis = _extract_diagnosis_from_chat(chat_messages)

    if raw_diagnosis is None:
        logger.error("Could not extract structured diagnosis from chat")
        return {
            "diagnosis": DiagnosisResult(
                failure_step="unknown",
                error_message="Agent failed to produce structured diagnosis",
                root_cause_category=RootCauseCategory.UNKNOWN,
                confidence=0.0,
                explanation="The multi-agent workflow did not produce a valid JSON diagnosis.",
                contributing_factors=["AutoGen GroupChat did not converge"],
                action_required="human_review",
            ).model_dump(),
            "fix_proposals": [],
            "action_taken": "escalated_to_human",
            "raw_chat_history": chat_messages,
        }

    # ── Parse into typed models ──────────────────────────────
    diag_raw = raw_diagnosis.get("diagnosis", {})

    # Map category string to enum (with fallback)
    category_str = diag_raw.get("root_cause_category", "unknown")
    try:
        category = RootCauseCategory(category_str.lower())
    except ValueError:
        category = RootCauseCategory.UNKNOWN

    confidence = float(diag_raw.get("confidence", 0.0))

    # Enforce the human_review threshold
    action_required = diag_raw.get("action_required")
    if confidence < 0.80:
        action_required = "human_review"

    diagnosis = DiagnosisResult(
        failure_step=diag_raw.get("failure_step", "unknown"),
        error_message=diag_raw.get("error_message", "No error message extracted"),
        root_cause_category=category,
        confidence=confidence,
        explanation=diag_raw.get("explanation", "No explanation provided"),
        contributing_factors=diag_raw.get("contributing_factors", []),
        action_required=action_required,
    )

    # Parse fix proposals
    fix_proposals: list[FixProposal] = []
    for fix_raw in raw_diagnosis.get("fix_proposals", []):
        try:
            risk_str = fix_raw.get("risk_level", "HIGH")
            risk = (
                RiskLevel(risk_str.upper())
                if isinstance(risk_str, str)
                else RiskLevel.HIGH
            )

            fix = FixProposal(
                description=fix_raw.get("description", "No description"),
                commands=fix_raw.get("commands", []),
                file_patches=fix_raw.get("file_patches"),
                risk_level=risk,
                success_probability=float(fix_raw.get("success_probability", 0.0)),
            )
            fix_proposals.append(fix)
        except Exception as exc:
            logger.warning("Failed to parse fix proposal: %s", exc)

    # Determine action taken
    if action_required == "human_review":
        action_taken = "escalated_to_human"
    elif (
        fix_proposals
        and fix_proposals[0].risk_level == RiskLevel.LOW
        and confidence >= 0.85
    ):
        action_taken = "auto_fix_eligible"
    else:
        action_taken = "awaiting_approval"

    logger.info(
        "Diagnosis complete — category=%s confidence=%.2f action=%s fixes=%d",
        category.value,
        confidence,
        action_taken,
        len(fix_proposals),
    )

    return {
        "diagnosis": diagnosis.model_dump(),
        "fix_proposals": [f.model_dump() for f in fix_proposals],
        "action_taken": action_taken,
        "raw_chat_history": chat_messages,
    }
