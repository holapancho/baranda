"""Level 2 for commands: decide whether an argv may run, and build the exact argv and env to run."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from fnmatch import fnmatchcase
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


def _neutralize_git_config(
    prefix: Sequence[str], env: dict[str, str], jail: Jail, patterns: Mapping[str, str], timeout: float
) -> None:
    """Force every config key matching ``patterns`` to its value, whatever driver name it uses.

    Attributes can select a driver from places no flag or env turns off (``$GIT_DIR/info/attributes``),
    so the drivers themselves are blanked. Their names are only known from the repository's config:
    list it with the same env and forced arguments the command will run with (same includes, same
    repository), then add the overrides through ``GIT_CONFIG_COUNT``, which takes the key verbatim
    (``-c`` would split a subsection name containing ``=``) and wins over repository config.
    """
    argv = ["git", *prefix, "config", "--list", "--name-only", "-z"]
    try:
        proc = subprocess.run(
            argv, cwd=jail.root, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        raise ToolError(ErrorKind.UNAVAILABLE, "timed out reading git config") from None
    except FileNotFoundError:
        raise ToolError(ErrorKind.UNAVAILABLE, "command not available: git") from None
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ToolError(ErrorKind.UNAVAILABLE, f"could not read git config: {detail}")

    keys = dict.fromkeys(k.decode("utf-8", errors="surrogateescape") for k in proc.stdout.split(b"\0") if k)
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    for key in keys:
        value = next((v for pattern, v in patterns.items() if fnmatchcase(key, pattern)), None)
        if value is not None:
            env[f"GIT_CONFIG_KEY_{count}"] = key
            env[f"GIT_CONFIG_VALUE_{count}"] = value
            count += 1
    if count:
        env["GIT_CONFIG_COUNT"] = str(count)


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

    prefix = [_render(a, jail) for a in rule.prefix_args]
    final = [binary, *prefix]
    if subcommand is not None:
        final.append(subcommand)
        final.extend(_render(a, jail) for a in rule.subcommand_args.get(subcommand, ()))
    final.extend(rest)

    env = _base_env()
    env.update({key: _render(value, jail) for key, value in rule.env.items()})
    if rule.neutralize_config:
        _neutralize_git_config(prefix, env, jail, rule.neutralize_config, policy.limits.timeout_seconds)

    return PreparedCommand(
        argv=tuple(final),
        env=env,
        cwd=jail.root,
        timeout=policy.limits.timeout_seconds,
        max_output_bytes=policy.limits.max_output_bytes,
        ok_exit_codes=rule.ok_exit_codes,
    )
