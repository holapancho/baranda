"""Expose the repo_reader agent over A2A.

    uv run python -m repo_reader.serve_a2a      (from the agents/ directory)

Agent card: http://127.0.0.1:8001/.well-known/agent-card.json

There is no authentication yet (phase 2: OIDC gateway), so it binds to 127.0.0.1 by default.
"""

from __future__ import annotations

import os

import uvicorn
from google.adk.a2a.utils.agent_to_a2a import to_a2a

from baranda.telemetry import configure as configure_telemetry

from .agent import build_agent


def create_app(host: str = "127.0.0.1", port: int = 8001):
    return to_a2a(build_agent(), host=host, port=port)


def main() -> None:
    host = os.environ.get("BARANDA_A2A_HOST", "127.0.0.1")
    port = int(os.environ.get("BARANDA_A2A_PORT", "8001"))
    configure_telemetry("baranda-agent-repo-reader")
    uvicorn.run(create_app(host, port), host=host, port=port)


if __name__ == "__main__":
    main()
