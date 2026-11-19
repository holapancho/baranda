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

Early development. `guard` (the policy layer) and the first connector,
[`repo_reader`](connectors/repo_reader/) (an MCP server for read-only repository exploration), are
implemented and tested. Docker images, the ADK agent and the demo come next.

[`docs/PLAN.md`](docs/PLAN.md) is the single source of truth: vision, decisions, architecture and plan.
Exploratory ideas live in [`docs/ideas/`](docs/ideas/).

## Development

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

```bash
uv run pytest
```

Using `guard` directly:

```python
from baranda import Guard

guard = Guard("/path/to/repo", ["connector.yaml", "policy/user.yaml"])
guard.run(["git", "log", "--oneline", "-5"])  # allowed by the connector's grant
guard.run(["rm", "-rf", "build"])             # raises Denied: "rm is not allowed (base)"
```

The built-in enforced policy ([`core/src/baranda/guard/base.yaml`](core/src/baranda/guard/base.yaml))
is always loaded first and cannot be removed by lower layers.

## License

[Apache-2.0](LICENSE)
