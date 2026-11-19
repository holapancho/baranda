# baranda

Deterministic guardrails for AI agents.

baranda exposes an agent's tools as **governed MCP servers**: every call goes through a `guard` that
decides **in code, not in the prompt** what may run. Agents (Google ADK) use those tools, reason
with them, and are exposed over **A2A** and REST/SSE.

What sets it apart:

1. CLI command policy at the level of binary, subcommand and flags.
2. Layered skills and policies (`enforced`, `default`, `optional`); lower levels can only restrict.
3. Short-lived credentials per user and per CLI (phase 2).
4. Memory per user, team and organization, with approval (phase 2).
5. Auditing and observability by design.

## Status

Design phase. No code yet.

[`docs/PLAN.md`](docs/PLAN.md) is the single source of truth: vision, decisions, architecture and plan.
Exploratory ideas live in [`docs/ideas/`](docs/ideas/).

## License

[Apache-2.0](LICENSE)
