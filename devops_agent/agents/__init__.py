"""
agents — Autonomous DevOps Pipeline Agent system.

Modules:
  coordinator      → AutoGen GroupChat orchestrator
  log_fetcher      → GitHub Actions log retrieval tool
  cloud_enricher   → GCP/AWS infrastructure context tool
  diagnosis        → AssistantAgent system prompt
  fix_generator    → LLM-powered fix proposal generation + history
  fix_executor     → Production-safe GitHub API fix execution
  slack_notifier   → Block Kit alerts (TYPE A failure / TYPE B resolution)
  validator        → Post-fix verification via GitHub polling
  notifier         → Legacy Slack notifier (deprecated)
  fixer            → Legacy git-clone fixer (deprecated)
  worker           → Redis consumer loop with retry logic
"""

from devops_agent.agents.coordinator import run_diagnosis_workflow
from devops_agent.agents.log_fetcher import fetch_github_logs
from devops_agent.agents.cloud_enricher import get_cloud_context
from devops_agent.agents.diagnosis import get_diagnosis_system_prompt
from devops_agent.agents.fix_generator import generate_fix_proposals, should_auto_apply
from devops_agent.agents.fix_executor import execute_fix
from devops_agent.agents.slack_notifier import SlackNotifier
from devops_agent.agents.validator import FixValidator

__all__ = [
    "run_diagnosis_workflow",
    "fetch_github_logs",
    "get_cloud_context",
    "get_diagnosis_system_prompt",
    "generate_fix_proposals",
    "should_auto_apply",
    "execute_fix",
    "SlackNotifier",
    "FixValidator",
]
