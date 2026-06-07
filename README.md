# 🤖 Autonomous DevOps Pipeline Agent

![CI/CD Pipeline](https://github.com/aman23-cmd/devOps_Agent/actions/workflows/ci.yml/badge.svg)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=flat&logo=fastapi)
![Docker](https://img.shields.io/badge/Docker-2496ED?style=flat&logo=docker&logoColor=white)
![Slack](https://img.shields.io/badge/Slack-4A154B?style=flat&logo=slack&logoColor=white)
![Anthropic](https://img.shields.io/badge/Anthropic-Claude-black.svg)
![Prometheus](https://img.shields.io/badge/Prometheus-E6522C?style=flat&logo=prometheus&logoColor=white)

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
- 🔄 **Dead Letter Queue (DLQ)**: Failed events that exhaust all retries are preserved in a Redis DLQ for inspection and replay — no data loss.
- ⏱️ **Rate Limiting**: Webhook endpoint is protected against abuse with configurable per-IP rate limits (30 req/min).
- 📈 **Prometheus Metrics**: Auto-instrumented HTTP metrics exposed at `/metrics` for Grafana/Prometheus integration.
- 📝 **Structured JSON Logging**: Production logs are emitted as structured JSON for seamless integration with ELK, Datadog, and CloudWatch.
- 🗄️ **Alembic Migrations**: Database schema changes are managed via Alembic for safe, reversible production deployments.

---

## 🏗️ Architecture Flow

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   GitHub     │───▶│  FastAPI      │───▶│    Redis     │───▶│   Worker     │
│   Webhook    │    │  /webhook     │    │   Queue      │    │  (AutoGen)   │
└──────────────┘    └──────┬───────┘    └──────────────┘    └──────┬───────┘
                           │                                        │
                    ┌──────▼───────┐                        ┌──────▼───────┐
                    │  Prometheus  │                        │   Claude AI   │
                    │  /metrics    │                        │  Diagnosis    │
                    └──────────────┘                        └──────┬───────┘
                                                                   │
                    ┌──────────────┐    ┌──────────────┐   ┌──────▼───────┐
                    │   Slack      │◀───│  PostgreSQL  │◀──│ Fix Generator │
                    │  Alerts      │    │  Audit Trail │   │ + Executor    │
                    └──────────────┘    └──────────────┘   └──────────────┘
```

1. **GitHub Webhook** ➔ Triggers FastAPI endpoint (`/webhook/github`) on pipeline failure.
2. **Rate Limiting** ➔ Protects against webhook abuse (30 req/min per IP).
3. **Message Queue** ➔ Event is enqueued in **Redis** for asynchronous processing.
4. **Agent Worker** ➔ Dequeues the event and coordinates the AI diagnosis.
5. **Fix Generator** ➔ Queries **PostgreSQL** for past fixes and uses **Claude/AutoGen** to propose solutions.
6. **Human Approval (Slack)** ➔ Sends an interactive Slack message. If the fix isn't whitelisted, it waits for a user to click "Apply Fix".
7. **Execution & Validation** ➔ Applies the fix via GitHub API, polls for the new pipeline run, and updates Slack with the final resolution (Success/Failure).
8. **Dead Letter Queue** ➔ Events that fail all 3 retries are preserved in the DLQ for replay.

---

## 🚀 Getting Started

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- Python 3.12+ (if running locally without Docker)
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

### 3. GitHub Webhook Setup

For the agent to receive events, you must configure a Webhook in your GitHub repository:
1. Go to your GitHub Repository ➔ **Settings** ➔ **Webhooks** ➔ **Add webhook**.
2. **Payload URL:** Your server's URL (e.g., `https://your-domain.com/webhook/github`). *If testing locally, use [ngrok](https://ngrok.com/) to expose port 8000.*
3. **Content type:** `application/json`
4. **Secret:** The same secret you set as `GITHUB_WEBHOOK_SECRET` in your `.env` file.
5. **Events:** Select "Let me select individual events" and check **Workflow runs**.

### 4. Run with Docker Compose (Recommended)

Start the entire stack (API, Worker, Redis, PostgreSQL):

```bash
docker-compose up --build -d
```

### 5. Run Locally (Development Mode)

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

### 6. Database Migrations (Alembic)

```bash
# Generate a new migration after model changes
alembic revision --autogenerate -m "describe your change"

# Apply all pending migrations
alembic upgrade head

# Rollback the last migration
alembic downgrade -1
```

---

## 📡 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Redirects to the dashboard |
| `GET` | `/health` | Liveness probe for container orchestrators |
| `GET` | `/status` | Agent health + analytics summary + DLQ depth |
| `GET` | `/status/recent?limit=20` | Most recent fix history records |
| `GET` | `/status/dlq` | View Dead Letter Queue contents |
| `POST` | `/status/dlq/replay?count=1` | Replay failed events from DLQ back to main queue |
| `POST` | `/webhook/github` | GitHub webhook ingress (requires `X-Hub-Signature-256`) |
| `POST` | `/slack/interact` | Interactive Slack button handler |
| `GET` | `/metrics` | Prometheus metrics endpoint |

---

## 🧪 Testing

The project uses `pytest` for the test suite, achieving 100% pass rate across 29 tests.

```bash
pytest tests/ -v
```

---

## 📈 Observability

### Prometheus Metrics

The agent exposes Prometheus metrics at `/metrics` including:
- HTTP request latency histograms
- Request count by status code and endpoint
- In-flight request gauge

Configure your Prometheus to scrape `http://localhost:8000/metrics`.

### Structured Logging

In production mode (`ENVIRONMENT=production`), all logs are emitted as structured JSON:

```json
{
  "timestamp": "2026-06-07T10:30:00",
  "level": "INFO",
  "logger": "agent_worker",
  "message": "Dequeued pipeline failure event",
  "module": "worker",
  "function": "_poll_loop",
  "line": 134
}
```

---

## 👨‍💻 Author

**Aman Kaushal**
- GitHub: [@aman23-cmd](https://github.com/aman23-cmd)
- LinkedIn: [Aman Kaushal](https://www.linkedin.com/in/aman-kaushal-b833642a0/)

---

## 📜 License

This project is licensed under the MIT License.
