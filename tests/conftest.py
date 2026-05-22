"""
Shared test configuration — ensures all required environment variables
are set before any module imports Settings.
"""

import os

# Set all required env vars BEFORE any test module imports Settings.
# This is done at module level so it happens before collection time.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-key-for-testing")
os.environ.setdefault("GITHUB_TOKEN", "ghp_test_token")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test_secret")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "slack_secret")
os.environ.setdefault("SLACK_CHANNEL_ID", "C12345")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("DATABASE_URL", "postgresql://devops:devops@localhost/devops")

# Clear the lru_cache so Settings picks up our test env vars
from config.settings import get_settings

get_settings.cache_clear()
