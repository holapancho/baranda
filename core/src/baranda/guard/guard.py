"""The ``Guard`` facade: the only way tools touch commands and files.

Level 1 (the tool, its caller and its arguments) is checked by the ``tool`` decorator.
Level 2 (each effect: command, read, list) is checked by ``run``/``execute``/``read``/``list_dir``.
Every decision is audited and traced. If guard cannot decide, it denies.
"""

from __future__ import annotations

import functools
import inspect
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence, TypeVar

from opentelemetry import trace

from . import commands, execute
from .audit import AuditLog
from .breaker import CircuitBreaker
from .errors import Denied, ErrorKind, PolicyError, ToolError
from .jail import Jail, matches_any
from .policy import Policy, PolicySource
from .redact import redact, sanitize

F = TypeVar("F", bound=Callable[..., Any])

BASE_POLICY_NAME = "base.yaml"
MAX_LIST_ENTRIES = 1000

_tracer = trace.get_tracer("baranda.guard")


@dataclass(frozen=True)
class CallContext:
    """Who is calling and through which tool. Set by the connector from the authenticated request,
    never from tool arguments."""

    user: str = "local"
    session: str | None = None
    tool: str | None = None


_context: ContextVar[CallContext] = ContextVar("baranda_call_context", default=CallContext())


def current_context() -> CallContext:
    return _context.get()


def base_policy_path() -> Path:
    """The built-in enforced policy shipped inside the package. It is always loaded first."""
    return Path(str(resources.files("baranda.guard").joinpath(BASE_POLICY_NAME)))


class Guard:
    def __init__(
        self,
        workspace: str | os.PathLike[str],
        policies: Sequence[str | os.PathLike[str]] = (),
        *,
        audit: AuditLog | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.jail = Jail(workspace)
        layers = [("base", base_policy_path())]
        layers += [(Path(p).name, Path(p)) for p in policies]
        self._source = PolicySource(layers)  # raises PolicyError: refuse to start
        self.audit = audit or AuditLog()
        self.breaker = breaker or CircuitBreaker()

    # --- context ---------------------------------------------------------------------------

    @contextmanager
    def context(self, *, user: str, session: str | None = None) -> Iterator[CallContext]:
        token = _context.set(replace(_context.get(), user=user, session=session))
        try:
            yield _context.get()
        finally:
            _context.reset(token)

    # --- policy ----------------------------------------------------------------------------

    @property
    def policy(self) -> Policy:
        return self._source.get()

    def _policy_or_deny(self) -> Policy:
        try:
            return self._source.get()
        except PolicyError:
            raise Denied("the policy could not be loaded; every action is denied until it is fixed") from None

    def _audit(self, *, effect: str, target: Any, decision: str, started: float, **extra: Any) -> None:
        ctx = _context.get()
        try:
            policy_hash = self._source.get().hash
        except PolicyError:
            policy_hash = None
        self.audit.record(
            user=ctx.user,
            session=ctx.session,
            tool=ctx.tool,
            effect=effect,
            target=target,
            decision=decision,
            policy=policy_hash,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            **extra,
        )

    # --- level 2: commands -----------------------------------------------------------------

    def check(self, argv: Sequence[str]) -> commands.PreparedCommand:
        """Decide without running. Raises ``Denied`` or ``ToolError``."""
        try:
            return commands.prepare(self._policy_or_deny(), self.jail, argv)
        except ToolError:
            raise
        except Exception:
            raise Denied("guard could not evaluate this command") from None

    def execute(self, argv: Sequence[str]) -> execute.CommandResult:
        started = time.perf_counter()
        target = [redact(a) for a in argv] if isinstance(argv, (list, tuple)) else redact(str(argv))
        binary = argv[0] if isinstance(argv, (list, tuple)) and argv else "?"
        with _tracer.start_as_current_span("guard.exec") as span:
            span.set_attribute("baranda.command", str(binary))
            try:
                prepared = self.check(argv)
            except ToolError as exc:
                span.set_attribute("baranda.decision", "deny" if exc.kind is ErrorKind.DENIED else "error")
                self._audit(effect="command", target=target, decision="deny" if exc.kind is ErrorKind.DENIED else "error",
                            started=started, kind=str(exc.kind), reason=exc.message)
                raise

            span.set_attribute("baranda.decision", "allow")
            try:
                result = execute.run(prepared)
            except ToolError as exc:
                self._audit(effect="command", target=target, decision="allow", started=started,
                            outcome="error", kind=str(exc.kind), reason=exc.message)
                raise

            ok = result.truncated or result.exit_code in prepared.ok_exit_codes
            span.set_attribute("baranda.exit_code", result.exit_code)
            self._audit(effect="command", target=target, decision="allow", started=started,
                        outcome="ok" if ok else "error", exit_code=result.exit_code, truncated=result.truncated)
            if not ok:
                detail = sanitize(result.stderr.strip() or result.stdout.strip() or "no output")
                raise ToolError(ErrorKind.INVALID_INPUT, f"{binary} exited with code {result.exit_code}: {detail}")
            return result

    def run(self, argv: Sequence[str]) -> str:
        """Run a command and return its stdout. Raises ``ToolError`` on denial or failure."""
        return self.execute(argv).stdout

    # --- level 2: files --------------------------------------------------------------------

    def _resolve_allowed(self, path: str, policy: Policy) -> Path:
        real = self.jail.resolve(path)
        pattern = matches_any(self.jail.relative(real), policy.deny_globs)
        if pattern:
            raise Denied(f"access to {path!r} is denied by pattern {pattern!r}")
        return real

    def read(self, path: str) -> str:
        started = time.perf_counter()
        with _tracer.start_as_current_span("guard.read"):
            try:
                policy = self._policy_or_deny()
                real = self._resolve_allowed(path, policy)
                if not real.exists():
                    raise ToolError(ErrorKind.INVALID_INPUT, f"file not found: {path}")
                if not real.is_file():
                    raise ToolError(ErrorKind.INVALID_INPUT, f"not a regular file: {path}")
                limit = policy.limits.max_read_bytes
                with real.open("rb") as fh:
                    data = fh.read(limit + 1)
            except ToolError as exc:
                self._audit(effect="read", target=redact(str(path)),
                            decision="deny" if exc.kind is ErrorKind.DENIED else "error",
                            started=started, kind=str(exc.kind), reason=exc.message)
                raise
            except OSError as exc:
                self._audit(effect="read", target=redact(str(path)), decision="error", started=started,
                            kind=str(ErrorKind.INVALID_INPUT), reason=exc.strerror)
                raise ToolError(ErrorKind.INVALID_INPUT, f"cannot read {path}: {exc.strerror}") from None

            truncated = len(data) > limit
            self._audit(effect="read", target=self.jail.relative(real), decision="allow", started=started,
                        outcome="ok", bytes=min(len(data), limit), truncated=truncated)
            text = data[:limit].decode("utf-8", errors="replace")
            return text + execute.TRUNCATED_MARKER if truncated else text

    def list_dir(self, path: str = ".") -> list[str]:
        started = time.perf_counter()
        with _tracer.start_as_current_span("guard.list"):
            try:
                policy = self._policy_or_deny()
                real = self._resolve_allowed(path, policy)
                if not real.is_dir():
                    raise ToolError(ErrorKind.INVALID_INPUT, f"not a directory: {path}")
                entries = sorted(real.iterdir(), key=lambda p: p.name)
            except ToolError as exc:
                self._audit(effect="list", target=redact(str(path)),
                            decision="deny" if exc.kind is ErrorKind.DENIED else "error",
                            started=started, kind=str(exc.kind), reason=exc.message)
                raise
            names = [e.name + ("/" if e.is_dir() and not e.is_symlink() else "") for e in entries[:MAX_LIST_ENTRIES]]
            if len(entries) > MAX_LIST_ENTRIES:
                names.append(f"… {len(entries) - MAX_LIST_ENTRIES} more entries")
            self._audit(effect="list", target=self.jail.relative(real), decision="allow", started=started,
                        outcome="ok", entries=len(entries))
            return names

    # --- level 1: tools --------------------------------------------------------------------

    def _check_tool(self, policy: Policy, name: str, arguments: Mapping[str, Any]) -> None:
        rule = policy.tools.get(name)
        if rule is None:
            return
        if not rule.enabled:
            raise Denied(f"tool {name} is disabled ({rule.disabled_by})", layer=rule.disabled_by)
        for arg, arg_rule in rule.args.items():
            if arg not in arguments or arguments[arg] is None:
                continue
            value = str(arguments[arg])
            if arg_rule.one_of is not None and value not in arg_rule.one_of:
                allowed = ", ".join(sorted(arg_rule.one_of)) or "(none)"
                raise Denied(f"{name}: {arg}={value!r} is not allowed; allowed: {allowed}")
            for pattern in arg_rule.patterns:
                if not re.fullmatch(pattern, value):
                    raise Denied(f"{name}: {arg}={value!r} does not match the allowed pattern")
            if arg_rule.max_length is not None and len(value) > arg_rule.max_length:
                raise Denied(f"{name}: {arg} is longer than {arg_rule.max_length} characters")

    def tool(self, name: str | None = None) -> Callable[[F], F]:
        """Decorate a tool function: level 1 check, context, tracing, breaker, error mapping."""

        def decorator(fn: F) -> F:
            tool_name = name or fn.__name__
            signature = inspect.signature(fn)

            def before(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[CallContext, float]:
                started = time.perf_counter()
                ctx = replace(_context.get(), tool=tool_name)
                key = (ctx.user, tool_name)
                self.breaker.check(key)
                token = _context.set(ctx)
                try:
                    try:
                        bound = signature.bind(*args, **kwargs)
                    except TypeError as exc:
                        raise ToolError(ErrorKind.INVALID_INPUT, f"{tool_name}: {exc}") from None
                    bound.apply_defaults()
                    try:
                        self._check_tool(self._policy_or_deny(), tool_name, bound.arguments)
                    except ToolError:
                        raise
                    except Exception:
                        raise Denied(f"guard could not evaluate tool {tool_name}") from None
                except ToolError as exc:
                    self.breaker.failure(key)
                    self._audit(effect="tool", target=tool_name,
                                decision="deny" if exc.kind is ErrorKind.DENIED else "error",
                                started=started, kind=str(exc.kind), reason=exc.message)
                    raise
                finally:
                    _context.reset(token)
                return ctx, started

            def after_error(ctx: CallContext, started: float, exc: BaseException) -> ToolError:
                key = (ctx.user, tool_name)
                self.breaker.failure(key)
                if isinstance(exc, ToolError):
                    error = exc
                    detail = exc.message
                else:
                    error = ToolError(ErrorKind.INTERNAL, f"{tool_name} failed with an internal error")
                    detail = sanitize(f"{type(exc).__name__}: {exc}")
                self._audit(effect="tool", target=tool_name, decision="allow", started=started,
                            outcome="error", kind=str(error.kind), reason=detail)
                return error

            def after_ok(ctx: CallContext, started: float) -> None:
                self.breaker.success((ctx.user, tool_name))
                self._audit(effect="tool", target=tool_name, decision="allow", started=started, outcome="ok")

            if inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    ctx, started = before(args, kwargs)
                    token = _context.set(ctx)
                    try:
                        with _tracer.start_as_current_span("guard.tool") as span:
                            span.set_attribute("baranda.tool", tool_name)
                            try:
                                result = await fn(*args, **kwargs)
                            except Exception as exc:
                                error = after_error(ctx, started, exc)
                                if error is exc:
                                    raise
                                raise error from exc
                        after_ok(ctx, started)
                        return result
                    finally:
                        _context.reset(token)

                return async_wrapper  # type: ignore[return-value]

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                ctx, started = before(args, kwargs)
                token = _context.set(ctx)
                try:
                    with _tracer.start_as_current_span("guard.tool") as span:
                        span.set_attribute("baranda.tool", tool_name)
                        try:
                            result = fn(*args, **kwargs)
                        except Exception as exc:
                            error = after_error(ctx, started, exc)
                            if error is exc:
                                raise
                            raise error from exc
                    after_ok(ctx, started)
                    return result
                finally:
                    _context.reset(token)

            return wrapper  # type: ignore[return-value]

        return decorator
