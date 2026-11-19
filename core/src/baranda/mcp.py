"""Register guard-governed tools on an MCP server (MCP Python SDK 2.x).

Requires the ``mcp`` extra: ``pip install "baranda[mcp]"``.
"""

from __future__ import annotations

import functools
import getpass
import inspect
import json
import os
from typing import Any, Callable, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from .guard import Guard, ToolError

F = TypeVar("F", bound=Callable[..., Any])


def local_user() -> str:
    """Identity for a local (stdio) server: ``BARANDA_USER`` or the OS user.

    Remote servers must take the identity from the validated access token instead.
    """
    return os.environ.get("BARANDA_USER") or getpass.getuser()


def error_result(error: ToolError) -> CallToolResult:
    """An is_error result whose text is exactly the JSON error, with no SDK prefix."""
    body = json.dumps(error.to_dict(), ensure_ascii=False)
    return CallToolResult(content=[TextContent(type="text", text=body)], is_error=True)


def tool(
    server: MCPServer,
    guard: Guard,
    name: str | None = None,
    *,
    user: Callable[[], str] = local_user,
    **tool_kwargs: Any,
) -> Callable[[F], F]:
    """Decorator: ``guard.tool()`` checks + MCP registration + structured errors.

    Usage::

        @tool(server, guard)
        def git_log(since: str = "1 week ago") -> str:
            return guard.run(["git", "log", f"--since={since}"])
    """

    def decorator(fn: F) -> F:
        governed = guard.tool(name)(fn)

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def adapter(*args: Any, **kwargs: Any) -> Any:
                with guard.context(user=user()):
                    try:
                        return await governed(*args, **kwargs)
                    except ToolError as exc:
                        return error_result(exc)

        else:

            @functools.wraps(fn)
            def adapter(*args: Any, **kwargs: Any) -> Any:
                with guard.context(user=user()):
                    try:
                        return governed(*args, **kwargs)
                    except ToolError as exc:
                        return error_result(exc)

        server.tool(name=name, **tool_kwargs)(adapter)
        return adapter  # type: ignore[return-value]

    return decorator
