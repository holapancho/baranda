# repo_reader connector

An MCP server (stdio) with read-only tools for exploring one git repository. Every tool and every
command goes through `guard`: the built-in `base.yaml`, this connector's
[`policy.yaml`](src/baranda_repo_reader/policy.yaml) and an optional user policy.

| Tool | What it does |
|---|---|
| `list_dir` | List a directory |
| `read_file` | Read a text file |
| `search` | ripgrep over the repository, excluding protected files |
| `git_log` | Commits since a date, optionally for one path |
| `git_show` | One commit, with stat or full patch |
| `git_blame` | Who last changed each line |
| `weekly_summary` | Commit count, commits per author, most changed files |
| `run` | Generic read-only command (allowlisted) |

Errors come back as an MCP error result whose text is JSON:
`{"error": {"kind": "denied", "message": "...", "retryable": false}}`.

## Run

```bash
uv run baranda-repo-reader --workspace /path/to/repo
```

Options (or environment variables): `--workspace` (`BARANDA_WORKSPACE`), `--user-policy`
(`BARANDA_USER_POLICY`), `--audit-log` (`BARANDA_AUDIT_LOG`). The identity recorded in the audit is
`BARANDA_USER` or the OS user.

Example MCP client configuration:

```json
{
  "mcpServers": {
    "repo-reader": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/baranda", "baranda-repo-reader", "--workspace", "/path/to/repo"]
    }
  }
}
```
