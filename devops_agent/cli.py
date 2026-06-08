"""
devops-agent CLI — Unified command-line interface for the DevOps Pipeline Agent.

Usage:
    devops-agent start-all                          Start both API and Worker (Single-Process)
    devops-agent api [--host HOST] [--port PORT]   Start the webhook API server
    devops-agent worker                             Start the background worker
    devops-agent status                             Check agent health
    devops-agent demo                               Send a test webhook event
    devops-agent --version                          Show version
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def cmd_api(args: argparse.Namespace) -> None:
    """Start the FastAPI webhook receiver."""
    import uvicorn

    print(f"Starting DevOps Agent API on {args.host}:{args.port}")
    uvicorn.run(
        "devops_agent.api.webhook_receiver:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


def cmd_worker(args: argparse.Namespace) -> None:
    """Start the background worker process."""
    from devops_agent.agents.worker import main as worker_main

    print("Starting DevOps Agent Worker...")
    try:
        asyncio.run(worker_main())
    except KeyboardInterrupt:
        print("\nWorker stopped")
        sys.exit(0)


async def run_start_all(host: str, port: int) -> None:
    """Run both the FastAPI server and Background Worker concurrently."""
    import uvicorn
    from devops_agent.agents.worker import AgentWorker

    config = uvicorn.Config(
        "devops_agent.api.webhook_receiver:app",
        host=host,
        port=port,
    )
    server = uvicorn.Server(config)
    worker = AgentWorker()

    print(f"[API] Listening on {host}:{port}")
    print("[Worker] Started")

    server_task = asyncio.create_task(server.serve())
    worker_task = asyncio.create_task(worker.start())

    try:
        # Wait for either task to complete (e.g. server exits due to signal)
        done, pending = await asyncio.wait(
            [server_task, worker_task], 
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Shut down the other task
        if server_task in pending:
            server.should_exit = True
            await server_task
            
        if worker_task in pending:
            worker._shutdown()
            await worker_task
            
    except asyncio.CancelledError:
        print("\nShutting down API and worker...")
        server.should_exit = True
        worker._shutdown()
        await asyncio.gather(server_task, worker_task, return_exceptions=True)


def cmd_start_all(args: argparse.Namespace) -> None:
    """Start both API and Worker in a single process."""
    try:
        asyncio.run(run_start_all(args.host, args.port))
    except KeyboardInterrupt:
        pass


def cmd_status(args: argparse.Namespace) -> None:
    """Query the local agent status."""
    import httpx

    url = f"http://{args.host}:{args.port}/status"
    try:
        response = httpx.get(url, timeout=5.0)
        data = response.json()

        print("╔══════════════════════════════════════════════╗")
        print("║      DevOps Pipeline Agent — Status          ║")
        print("╚══════════════════════════════════════════════╝")
        print(f"  Status:     {data.get('status', 'unknown')}")
        print(f"  Version:    {data.get('version', 'N/A')}")
        print(f"  DLQ Depth:  {data.get('dlq_depth', 0)}")

        analytics = data.get("analytics", {})
        if "error" not in analytics:
            total = analytics.get("total_fixes", 0)
            rate = analytics.get("success_rate", 0)
            print(f"  Total Fixes: {total}")
            print(f"  Success Rate: {rate:.1%}")
            avg = analytics.get("avg_duration_seconds")
            if avg:
                print(f"  Avg Duration: {avg:.1f}s")
        else:
            print(f"  Analytics: {analytics['error']}")

    except httpx.ConnectError:
        print(f"❌ Could not connect to agent at {url}")
        print("   Make sure the API is running: devops-agent api")
        sys.exit(1)


def cmd_demo(args: argparse.Namespace) -> None:
    """Send a test webhook event to the local agent."""
    import hmac
    import hashlib
    import json
    import os
    import random
    import time

    try:
        import httpx
    except ImportError:
        print("❌ httpx required. Install with: pip install httpx")
        sys.exit(1)

    from dotenv import load_dotenv

    load_dotenv()

    secret = os.getenv("GITHUB_WEBHOOK_SECRET")
    if not secret:
        print("❌ GITHUB_WEBHOOK_SECRET not found in .env file")
        sys.exit(1)

    run_id = random.randint(100000, 999999)
    repo = args.repo

    payload = {
        "action": "completed",
        "workflow_run": {
            "id": run_id,
            "conclusion": "failure",
            "head_branch": "demo-branch",
            "head_sha": f"deadbeef{random.randint(1000, 9999)}",
            "name": "CI/CD Pipeline",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "html_url": f"https://github.com/{repo}/actions/runs/{run_id}",
        },
        "repository": {
            "full_name": repo,
            "html_url": f"https://github.com/{repo}",
        },
    }

    payload_bytes = json.dumps(payload).encode("utf-8")
    signature = (
        "sha256="
        + hmac.new(
            key=secret.encode("utf-8"),
            msg=payload_bytes,
            digestmod=hashlib.sha256,
        ).hexdigest()
    )

    url = f"http://{args.host}:{args.port}/webhook/github"
    print(f"Sending test failure event to {url}")
    print(f"   Repo: {repo}  |  Run ID: {run_id}")

    try:
        response = httpx.post(
            url,
            content=payload_bytes,
            headers={
                "X-Hub-Signature-256": signature,
                "X-GitHub-Event": "workflow_run",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )

        if response.status_code == 200:
            data = response.json()
            print(f"Event enqueued — {data}")
            print(f"Check your dashboard: http://{args.host}:{args.port}/dashboard/")
        else:
            print(f"❌ Failed: {response.status_code} — {response.text}")
    except httpx.ConnectError:
        print(f"❌ Could not connect to {url}")
        print("   Make sure the API is running: devops-agent api")
        sys.exit(1)


def main() -> None:
    """CLI entry point."""
    from devops_agent import __version__

    parser = argparse.ArgumentParser(
        prog="devops-agent",
        description="DevOps Pipeline Agent — AI-powered CI/CD failure diagnosis & auto-fix",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  devops-agent start-all              Start both API and Worker concurrently\n"
            "  devops-agent api                    Start the webhook receiver\n"
            "  devops-agent api --port 9000        Start on custom port\n"
            "  devops-agent worker                 Start the background worker\n"
            "  devops-agent status                 Check agent health\n"
            "  devops-agent demo                   Send a test failure event\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"devops-agent {__version__}")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ── start-all ──
    start_all_parser = subparsers.add_parser("start-all", help="Start both API and Worker (Single Process)")
    start_all_parser.add_argument("--host", default="0.0.0.0", help="Bind host for API (default: 0.0.0.0)")
    start_all_parser.add_argument("--port", type=int, default=8000, help="Bind port for API (default: 8000)")
    start_all_parser.set_defaults(func=cmd_start_all)

    # ── api ──
    api_parser = subparsers.add_parser("api", help="Start the webhook API server")
    api_parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    api_parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    api_parser.add_argument("--reload", action="store_true", help="Enable auto-reload (dev mode)")
    api_parser.set_defaults(func=cmd_api)

    # ── worker ──
    worker_parser = subparsers.add_parser("worker", help="Start the background worker")
    worker_parser.set_defaults(func=cmd_worker)

    # ── status ──
    status_parser = subparsers.add_parser("status", help="Check agent health & analytics")
    status_parser.add_argument("--host", default="localhost", help="API host (default: localhost)")
    status_parser.add_argument("--port", type=int, default=8000, help="API port (default: 8000)")
    status_parser.set_defaults(func=cmd_status)

    # ── demo ──
    demo_parser = subparsers.add_parser("demo", help="Send a test webhook event")
    demo_parser.add_argument("--repo", default="aman23-cmd/devOps_Agent", help="Target repo name")
    demo_parser.add_argument("--host", default="localhost", help="API host (default: localhost)")
    demo_parser.add_argument("--port", type=int, default=8000, help="API port (default: 8000)")
    demo_parser.set_defaults(func=cmd_demo)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
