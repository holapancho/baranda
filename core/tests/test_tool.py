import asyncio

import pytest

from baranda.guard import CircuitBreaker, Denied, ErrorKind, Guard, ToolError, current_context
from baranda.guard.redact import redact

from .conftest import write

TOOLS = """\
mode: restrict
tools:
  sf_query:
    args:
      org: {one_of: [dev, uat]}
      soql: {pattern: "(?is)\\\\s*select\\\\b.*", max_length: 200}
  dangerous:
    enabled: false
"""


@pytest.fixture
def tool_guard(repo, policy_dir, audit):
    write(policy_dir / "user.yaml", TOOLS)
    return Guard(repo, [policy_dir / "connector.yaml", policy_dir / "user.yaml"], audit=audit)


def test_tool_runs_and_sees_its_context(tool_guard, audit):
    @tool_guard.tool()
    def git_log() -> str:
        assert current_context().tool == "git_log"
        return tool_guard.run(["git", "log", "--format=%s", "-1"])

    with tool_guard.context(user="u1", session="s1"):
        assert git_log() == "say hello world\n"

    command, tool = audit.records[-2:]
    assert (command["effect"], command["tool"], command["user"], command["session"]) == ("command", "git_log", "u1", "s1")
    assert (tool["effect"], tool["outcome"]) == ("tool", "ok")
    assert current_context().tool is None


def test_disabled_tool_is_denied(tool_guard):
    @tool_guard.tool()
    def dangerous() -> str:
        raise AssertionError("must not run")

    with pytest.raises(Denied, match="disabled"):
        dangerous()


@pytest.mark.parametrize(
    ("org", "soql", "ok"),
    [
        ("dev", "SELECT Id FROM Account", True),
        ("prod", "SELECT Id FROM Account", False),
        ("dev", "DELETE FROM Account", False),
        ("dev", "SELECT " + "x" * 300, False),
    ],
)
def test_argument_rules(tool_guard, org, soql, ok):
    calls = []

    @tool_guard.tool()
    def sf_query(org: str, soql: str) -> str:
        calls.append((org, soql))
        return "ok"

    if ok:
        assert sf_query(org, soql=soql) == "ok"
    else:
        with pytest.raises(Denied):
            sf_query(org, soql=soql)
        assert calls == []


def test_internal_errors_do_not_leak(tool_guard, audit):
    @tool_guard.tool()
    def broken() -> str:
        raise RuntimeError("db password=hunter2 at 10.0.0.1")

    with pytest.raises(ToolError) as info:
        broken()
    assert info.value.kind is ErrorKind.INTERNAL
    assert "hunter2" not in info.value.message
    assert "hunter2" not in audit.records[-1]["reason"]


def test_bad_arguments_are_invalid_input(tool_guard):
    @tool_guard.tool()
    def needs_one(x: str) -> str:
        return x

    with pytest.raises(ToolError) as info:
        needs_one()
    assert info.value.kind is ErrorKind.INVALID_INPUT


def test_async_tools(tool_guard):
    @tool_guard.tool()
    async def git_status() -> str:
        return tool_guard.run(["git", "status", "--short"])

    assert asyncio.run(git_status()) == ""


def test_circuit_breaker_opens_and_recovers(repo, policy_dir):
    now = [0.0]
    guard = Guard(
        repo,
        [policy_dir / "connector.yaml"],
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=60, clock=lambda: now[0]),
    )
    attempts = []

    @guard.tool()
    def flaky() -> str:
        attempts.append(1)
        return guard.run(["rm", "-rf", "/"])

    for _ in range(3):
        with pytest.raises(Denied):
            flaky()
    with pytest.raises(ToolError) as info:
        flaky()
    assert info.value.kind is ErrorKind.CIRCUIT_OPEN
    assert len(attempts) == 3

    now[0] = 61
    with pytest.raises(Denied):
        flaky()  # half-open: one more try, fails, reopens
    with pytest.raises(ToolError) as info:
        flaky()
    assert info.value.kind is ErrorKind.CIRCUIT_OPEN


def test_breaker_is_per_user(repo, policy_dir):
    guard = Guard(repo, [policy_dir / "connector.yaml"], breaker=CircuitBreaker(threshold=1))

    @guard.tool()
    def bad() -> str:
        return guard.run(["rm", "x"])

    with guard.context(user="a"), pytest.raises(Denied):
        bad()
    with guard.context(user="a"), pytest.raises(ToolError, match="failed 1 times"):
        bad()
    with guard.context(user="b"), pytest.raises(Denied):
        bad()


def test_error_shape():
    error = ToolError(ErrorKind.RATE_LIMITED, "slow down", status=429, retryable=True)
    assert error.to_dict() == {"error": {"kind": "rate_limited", "message": "slow down", "retryable": True, "status": 429}}


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "a" * 36,
        "AKIA" + "B" * 16,
        "AIza" + "c" * 35,
        "sk-" + "d" * 40,
        "Authorization: Bearer eyJhbGciOi",
        "password=hunter2",
    ],
)
def test_redact(secret):
    assert "REDACTED" in redact(f"error: {secret} end")
