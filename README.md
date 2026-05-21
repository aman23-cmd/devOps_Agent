# 🤖 Autonomous DevOps Pipeline Agent

![CI/CD Pipeline](https://github.com/aman23-cmd/devOps_Agent/actions/workflows/ci.yml/badge.svg)
![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=flat&logo=fastapi)
![Docker](https://img.shields.io/badge/Docker-2496ED?style=flat&logo=docker&logoColor=white)
![Slack](https://img.shields.io/badge/Slack-4A154B?style=flat&logo=slack&logoColor=white)
![Anthropic](https://img.shields.io/badge/Anthropic-Claude-black.svg)

An intelligent, event-driven DevOps assistant that autonomously monitors your CI/CD pipelines, diagnoses failures using Large Language Models (LLMs), and automatically suggests and applies fixes. Designed for modern engineering teams to reduce mean time to recovery (MTTR) and eliminate manual pipeline babysitting.

---

## ✨ Key Features

- 🎧 **Real-Time Webhook Listening**: Captures GitHub Actions `workflow_run` failure events securely via HMAC-SHA256 verification.
- 🧠 **AI-Powered Diagnosis & Fix Generator**: Uses Anthropic's Claude to analyze failure logs, cross-reference historical fixes (PostgreSQL), and rank 3-5 potential fix proposals.
- 🛡️ **Auto-Fix Whitelist**: Low-risk failures (e.g., `FLAKY_TEST`, `NETWORK_TIMEOUT`) are automatically resolved and retried without human intervention.
- 💬 **Interactive Slack Integration**: Rich Block Kit messages for failure alerts and fix approvals. Features "Apply Fix" and "Retry Pipeline" buttons for human-in-the-loop decision making.
- 🛠️ **Secure Fix Execution**: Zero local shell execution. All fixes are applied strictly via the GitHub REST API (patch commits, PRs, and workflow reruns).
- 📊 **Analytics & Audit Trail**: Full history of failures, AI confidence scores, fix durations, and success rates stored in PostgreSQL. Accessible via `/status` API.
- 🐳 **Production-Ready**: Containerized with a multi-stage Dockerfile, orchestrated via Docker Compose (Agent + Redis + PostgreSQL).

---

## 🏗️ Architecture Flow

1. **GitHub Webhook** ➔ Triggers FastAPI endpoint (`/webhook/github`) on pipeline failure.
2. **Message Queue** ➔ Event is enqueued in **Redis** for asynchronous processing.
3. **Agent Worker** ➔ Dequeues the event and coordinates the AI diagnosis.
4. **Fix Generator** ➔ Queries **PostgreSQL** for past fixes and uses **Claude/AutoGen** to propose solutions.
5. **Human Approval (Slack)** ➔ Sends an interactive Slack message. If the fix isn't whitelisted, it waits for a user to click "Apply Fix".
6. **Execution & Validation** ➔ Applies the fix via GitHub API, polls for the new pipeline run, and updates Slack with the final resolution (Success/Failure).

---

## 🚀 Getting Started

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- Python 3.13+ (if running locally without Docker)
- GitHub Personal Access Token (with repo scope)
- Slack App (Bot Token & Signing Secret)
- Anthropic API Key

### 1. Clone the repository

```bash
git clone https://github.com/aman23-cmd/devOps_Agent.git
cd devOps_Agent
```

### 2. Environment Configuration

Copy the example environment file and fill in your secrets:

```bash
cp .env.example .env
```
*(Make sure to add your `GITHUB_TOKEN`, `SLACK_BOT_TOKEN`, and `ANTHROPIC_API_KEY`)*

### 3. Run with Docker Compose (Recommended)

Start the entire stack (API, Worker, Redis, PostgreSQL):

```bash
docker-compose up --build -d
```

### 4. Run Locally (Development Mode)

If you prefer to run it without Docker:

```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Start the FastAPI server
uvicorn api.webhook_receiver:app --reload

# In a separate terminal, start the worker
python -m agents.worker
```

---

## 📡 API Endpoints

- **`GET /status`**
  Returns overall agent health and an analytics summary (success rate, average fix duration, categories).

- **`GET /status/recent?limit=20`**
  Returns the most recent fix history records.

- **`POST /webhook/github`**
  Ingress for GitHub webhook payloads. Requires `X-Hub-Signature-256`.

- **`POST /slack/interact`**
  Handles interactive Slack button clicks. Requires `X-Slack-Signature`.

---

## 🧪 Testing

The project uses `pytest` for the test suite, achieving 100% pass rate across 29 tests.

```bash
pytest tests/ -v
```

---

## 📜 License

This project is licensed under the MIT License.
