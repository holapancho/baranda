"""Level 2 for commands: decide whether an argv may run, and build the exact argv and env to run."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .errors import Denied, ErrorKind, ToolError
from .jail import Jail, matches_any
from .policy import Policy

# Fixed, never inherited from the caller. On macOS, Homebrew (Apple Silicon) comes first so its
# newer git is preferred over the system one.
DEFAULT_PATH = ("/opt/homebrew/bin:" if sys.platform == "darwin" else "") + "/usr/local/bin:/usr/bin:/bin"
MAX_ARGS = 64
MAX_ARG_LENGTH = 4096


@dataclass(frozen=True)
class PreparedCommand:
    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path
    timeout: float
    max_output_bytes: int
    ok_exit_codes: frozenset[int]


def _base_env() -> dict[str, str]:
    return {
        "PATH": DEFAULT_PATH,
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _render(value: str, jail: Jail) -> str:
    return value.replace("{root}", str(jail.root))


def _check_path(value: str, jail: Jail, policy: Policy) -> None:
    """Deny path-like values that resolve outside the workspace or onto a denied file."""
    if "\x00" in value:
        raise Denied("argument contains a NUL byte")
    candidate = jail.root / value
    looks_like_path = (
        value.startswith("/")
        or ".." in value.split("/")
        or candidate.exists()
        or candidate.is_symlink()
    )
    if not looks_like_path:
        return
    real = jail.resolve(value)
    pattern = matches_any(jail.relative(real), policy.deny_globs)
    if pattern:
        raise Denied(f"access to {value!r} is denied by pattern {pattern!r}")


def _check_flag(arg: str, deny_flags: Mapping[str, str], binary: str) -> None:
    if arg.startswith("--"):
        name = arg.split("=", 1)[0]
        if name in deny_flags:
            raise Denied(f"flag {name} is not allowed for {binary} ({deny_flags[name]})", layer=deny_flags[name])
        return
    # Short flags, possibly clustered: -uL, -Ofile.
    if arg in deny_flags:
        raise Denied(f"flag {arg} is not allowed for {binary} ({deny_flags[arg]})", layer=deny_flags[arg])
    for char in arg[1:]:
        flag = f"-{char}"
        if flag in deny_flags:
            raise Denied(f"flag {flag} is not allowed for {binary} ({deny_flags[flag]})", layer=deny_flags[flag])


def prepare(policy: Policy, jail: Jail, argv: Sequence[str]) -> PreparedCommand:
    if isinstance(argv, str) or not isinstance(argv, Sequence) or not argv:
        raise ToolError(ErrorKind.INVALID_INPUT, "argv must be a non-empty list of strings")
    if not all(isinstance(arg, str) for arg in argv):
        raise ToolError(ErrorKind.INVALID_INPUT, "argv must be a non-empty list of strings")
    if len(argv) > MAX_ARGS or any(len(arg) > MAX_ARG_LENGTH for arg in argv):
        raise ToolError(ErrorKind.INVALID_INPUT, f"argv is limited to {MAX_ARGS} arguments of {MAX_ARG_LENGTH} chars")

    binary = argv[0]
    if "/" in binary or "\x00" in binary:
        raise Denied("commands must be given by name, not by path")
    if binary in policy.deny_commands:
        layer = policy.deny_commands[binary]
        raise Denied(f"{binary} is not allowed ({layer})", layer=layer)
    rule = policy.commands.get(binary)
    if rule is None:
        raise Denied(f"{binary} is not in the allowlist")

    rest = list(argv[1:])
    subcommand: str | None = None
    if rule.subcommands is not None:
        if not rest or rest[0] not in rule.subcommands:
            given = rest[0] if rest else "(none)"
            allowed = ", ".join(sorted(rule.subcommands)) or "(none)"
            raise Denied(f"{binary} {given} is not allowed; allowed subcommands: {allowed}", layer=rule.granted_by)
        subcommand = rest.pop(0)

    end_of_options = False
    value_is_data = False  # the previous arg was a non-path flag that takes the next arg as its value
    for arg in rest:
        if value_is_data:
            value_is_data = False
        elif end_of_options:
            _check_path(arg, jail, policy)
        elif arg == "--":
            end_of_options = True
        elif arg.startswith("-") and arg != "-":
            _check_flag(arg, rule.deny_flags, binary)
            name, has_value, value = arg.partition("=")
            if arg in rule.non_path_flags:
                value_is_data = True
            elif arg.startswith("--") and has_value and name not in rule.non_path_flags:
                _check_path(value, jail, policy)
        else:
            _check_path(arg, jail, policy)

    final = [binary, *(_render(a, jail) for a in rule.prefix_args)]
    if subcommand is not None:
        final.append(subcommand)
        final.extend(_render(a, jail) for a in rule.subcommand_args.get(subcommand, ()))
    final.extend(rest)

    env = _base_env()
    env.update({key: _render(value, jail) for key, value in rule.env.items()})

    return PreparedCommand(
        argv=tuple(final),
        env=env,
        cwd=jail.root,
        timeout=policy.limits.timeout_seconds,
        max_output_bytes=policy.limits.max_output_bytes,
        ok_exit_codes=rule.ok_exit_codes,
    )
