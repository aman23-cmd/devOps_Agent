import hmac
import hashlib
import json
import time
import random
import os
import requests
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")
if not WEBHOOK_SECRET:
    print("❌ ERROR: GITHUB_WEBHOOK_SECRET not found in .env file.")
    exit(1)

WEBHOOK_URL = "http://localhost:8000/webhook/github"


def trigger_fake_failure():
    print("🚀 Triggering Chaos Automation Demo...")

    # Generate random run ID and error type for variety
    run_id = random.randint(100000, 999999)
    repo_name = "aman23-cmd/devOps_Agent"

    error_types = [
        "Code Regression (pytest failed)",
        "Dependency Issue (npm ERR!)",
        "Flaky Test (timeout)",
    ]
    error_choice = random.choice(error_types)

    print(f"📦 Target Repo: {repo_name}")
    print(f"🔥 Simulated Error: {error_choice}")

    # Simulated GitHub Webhook Payload for a failed workflow run
    payload = {
        "action": "completed",
        "workflow_run": {
            "id": run_id,
            "conclusion": "failure",
            "head_branch": "demo-chaos-branch",
            "head_sha": f"deadbeef{random.randint(1000, 9999)}",
            "name": "CI/CD Pipeline",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "html_url": f"https://github.com/{repo_name}/actions/runs/{run_id}",
        },
        "repository": {
            "full_name": repo_name,
            "html_url": f"https://github.com/{repo_name}",
        },
    }

    payload_bytes = json.dumps(payload).encode("utf-8")

    # Sign payload exactly like GitHub does
    signature = (
        "sha256="
        + hmac.new(
            key=WEBHOOK_SECRET.encode("utf-8"),
            msg=payload_bytes,
            digestmod=hashlib.sha256,
        ).hexdigest()
    )

    headers = {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Event": "workflow_run",
        "Content-Type": "application/json",
    }

    print("\n📡 Sending Webhook to DevOps Agent...")

    try:
        response = requests.post(WEBHOOK_URL, data=payload_bytes, headers=headers)
        if response.status_code == 200:
            print("✅ Success! Webhook accepted.")
            print("Response:", response.json())
            print(
                "\n👀 Now go check your Live Dashboard at http://localhost:8000/dashboard/"
            )
            print(
                "You should see the new failure pop up and the agent will start fixing it!"
            )
        else:
            print(f"❌ Failed. Status Code: {response.status_code}")
            print("Response:", response.text)
    except requests.exceptions.ConnectionError:
        print("❌ ERROR: Could not connect to the webhook receiver.")
        print(
            "Make sure your FastAPI server is running: python -m uvicorn api.webhook_receiver:app --reload"
        )


if __name__ == "__main__":
    trigger_fake_failure()
