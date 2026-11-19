"""Policy layers: loading, validation and merging.

Three layer modes:

- ``enforced``: the built-in base. Denies, forces arguments and environment, caps limits.
- ``grant``: a connector's policy. The only mode that can allow commands.
- ``restrict``: a user's (or team's) policy. Can only take things away.

What each mode may contain is checked structurally, so a ``restrict`` layer cannot widen access by
construction. Deny always wins.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import yaml

from .errors import PolicyError


class LayerMode(StrEnum):
    ENFORCED = "enforced"
    GRANT = "grant"
    RESTRICT = "restrict"


@dataclass(frozen=True)
class ArgRule:
    one_of: frozenset[str] | None = None
    patterns: tuple[str, ...] = ()
    max_length: int | None = None


@dataclass(frozen=True)
class ToolRule:
    enabled: bool = True
    disabled_by: str | None = None
    args: Mapping[str, ArgRule] = field(default_factory=dict)
    layers: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandRule:
    granted_by: str
    subcommands: frozenset[str] | None = None
    deny_flags: Mapping[str, str] = field(default_factory=dict)  # flag -> layer
    prefix_args: tuple[str, ...] = ()
    subcommand_args: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    ok_exit_codes: frozenset[int] = frozenset({0})


@dataclass(frozen=True)
class Limits:
    timeout_seconds: float = 30.0
    max_output_bytes: int = 1_048_576
    max_read_bytes: int = 1_048_576

    def capped(self, other: Mapping[str, Any]) -> Limits:
        values = {}
        for key in ("timeout_seconds", "max_output_bytes", "max_read_bytes"):
            if key in other:
                values[key] = min(getattr(self, key), other[key])
        return replace(self, **values)


@dataclass(frozen=True)
class Layer:
    name: str
    mode: LayerMode
    data: Mapping[str, Any]


@dataclass(frozen=True)
class Policy:
    commands: Mapping[str, CommandRule]
    deny_commands: Mapping[str, str]  # command -> layer
    deny_globs: tuple[str, ...]
    tools: Mapping[str, ToolRule]
    limits: Limits
    layers: tuple[str, ...]
    hash: str


# --- validation -----------------------------------------------------------------------------

_TOP_KEYS = {
    LayerMode.ENFORCED: {"mode", "description", "deny_commands", "commands", "tools", "limits", "paths"},
    LayerMode.GRANT: {"mode", "description", "deny_commands", "commands", "tools", "limits", "paths"},
    LayerMode.RESTRICT: {
        "mode",
        "description",
        "deny_commands",
        "only_commands",
        "commands",
        "tools",
        "limits",
        "paths",
    },
}
_COMMAND_KEYS = {
    LayerMode.ENFORCED: {"deny_flags", "prefix_args", "subcommand_args", "env"},
    LayerMode.GRANT: {"subcommands", "deny_flags", "env", "ok_exit_codes"},
    LayerMode.RESTRICT: {"subcommands", "deny_flags"},
}
_TOOL_KEYS = {"enabled", "args"}
_ARG_KEYS = {"one_of", "pattern", "max_length"}
_LIMIT_KEYS = {"timeout_seconds", "max_output_bytes", "max_read_bytes"}
_PATH_KEYS = {"deny_globs"}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def _fail(layer: str, message: str) -> PolicyError:
    return PolicyError(f"{layer}: {message}")


def _check_keys(layer: str, where: str, value: Any, allowed: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(layer, f"{where} must be a mapping")
    unknown = set(value) - allowed
    if unknown:
        raise _fail(layer, f"{where} has keys not allowed here: {sorted(unknown)}")
    return value


def _str_list(layer: str, where: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise _fail(layer, f"{where} must be a list of non-empty strings")
    return value


def _positive(layer: str, where: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise _fail(layer, f"{where} must be a positive number")
    return value


def _validate(layer: Layer) -> None:
    name, mode, data = layer.name, layer.mode, layer.data
    _check_keys(name, "policy", data, _TOP_KEYS[mode])

    for key in ("deny_commands", "only_commands"):
        if key in data:
            for command in _str_list(name, key, data[key]):
                if not _NAME.match(command):
                    raise _fail(name, f"{key} has an invalid command name: {command!r}")

    for command, rule in (data.get("commands") or {}).items():
        if not isinstance(command, str) or not _NAME.match(command):
            raise _fail(name, f"invalid command name: {command!r}")
        where = f"commands.{command}"
        rule = _check_keys(name, where, rule or {}, _COMMAND_KEYS[mode])
        for key in ("subcommands", "deny_flags", "prefix_args"):
            if key in rule:
                _str_list(name, f"{where}.{key}", rule[key])
        for flag in rule.get("deny_flags", []):
            if not flag.startswith("-"):
                raise _fail(name, f"{where}.deny_flags entries must start with '-': {flag!r}")
        if "subcommand_args" in rule:
            sub_args = _check_keys(name, f"{where}.subcommand_args", rule["subcommand_args"], set(rule["subcommand_args"]))
            for sub, args in sub_args.items():
                _str_list(name, f"{where}.subcommand_args.{sub}", args)
        if "env" in rule:
            env = _check_keys(name, f"{where}.env", rule["env"], set(rule["env"]))
            for key, value in env.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise _fail(name, f"{where}.env must map strings to strings")
        if "ok_exit_codes" in rule:
            codes = rule["ok_exit_codes"]
            if not isinstance(codes, list) or not all(type(c) is int for c in codes):
                raise _fail(name, f"{where}.ok_exit_codes must be a list of integers")

    for tool, rule in (data.get("tools") or {}).items():
        where = f"tools.{tool}"
        rule = _check_keys(name, where, rule or {}, _TOOL_KEYS)
        if "enabled" in rule and not isinstance(rule["enabled"], bool):
            raise _fail(name, f"{where}.enabled must be true or false")
        if mode is not LayerMode.GRANT and rule.get("enabled") is True:
            raise _fail(name, f"{where}.enabled: only grant layers may enable tools")
        for arg, arg_rule in (rule.get("args") or {}).items():
            arg_where = f"{where}.args.{arg}"
            arg_rule = _check_keys(name, arg_where, arg_rule or {}, _ARG_KEYS)
            if "one_of" in arg_rule:
                _str_list(name, f"{arg_where}.one_of", arg_rule["one_of"])
            if "pattern" in arg_rule:
                try:
                    re.compile(arg_rule["pattern"])
                except (re.error, TypeError) as exc:
                    raise _fail(name, f"{arg_where}.pattern is not a valid regex: {exc}") from None
            if "max_length" in arg_rule:
                _positive(name, f"{arg_where}.max_length", arg_rule["max_length"])

    limits = _check_keys(name, "limits", data.get("limits") or {}, _LIMIT_KEYS)
    for key, value in limits.items():
        _positive(name, f"limits.{key}", value)

    paths = _check_keys(name, "paths", data.get("paths") or {}, _PATH_KEYS)
    if "deny_globs" in paths:
        _str_list(name, "paths.deny_globs", paths["deny_globs"])


# --- loading --------------------------------------------------------------------------------


def load_layer(path: str | os.PathLike[str], name: str | None = None) -> Layer:
    path = Path(path)
    layer_name = name or path.name
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise _fail(layer_name, f"cannot read policy file: {exc}") from None
    except yaml.YAMLError as exc:
        raise _fail(layer_name, f"invalid YAML: {exc}") from None
    return parse_layer(data, layer_name)


def parse_layer(data: Any, name: str) -> Layer:
    if not isinstance(data, Mapping):
        raise _fail(name, "policy must be a mapping")
    try:
        mode = LayerMode(data.get("mode"))
    except ValueError:
        raise _fail(name, f"mode must be one of {[m.value for m in LayerMode]}") from None
    layer = Layer(name=name, mode=mode, data=data)
    _validate(layer)
    return layer


# --- merging --------------------------------------------------------------------------------


def _layer_hash(layers: Sequence[Layer]) -> str:
    canonical = json.dumps(
        [[layer.name, layer.mode.value, layer.data] for layer in layers],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def merge(layers: Sequence[Layer]) -> Policy:
    """Merge layers into one effective policy. Order matters only for display and env override."""
    commands: dict[str, CommandRule] = {}
    deny_commands: dict[str, str] = {}
    deny_globs: list[str] = []
    tools: dict[str, ToolRule] = {}
    limits = Limits()

    # 1. Grants define what exists.
    for layer in layers:
        if layer.mode is not LayerMode.GRANT:
            continue
        for command, raw in (layer.data.get("commands") or {}).items():
            raw = raw or {}
            if command in commands:
                raise _fail(layer.name, f"command {command!r} is already granted by {commands[command].granted_by}")
            subcommands = raw.get("subcommands")
            commands[command] = CommandRule(
                granted_by=layer.name,
                subcommands=frozenset(subcommands) if subcommands is not None else None,
                deny_flags={flag: layer.name for flag in raw.get("deny_flags", [])},
                env=dict(raw.get("env", {})),
                ok_exit_codes=frozenset(raw.get("ok_exit_codes", [0])),
            )

    # 2. Every layer can deny, restrict and cap.
    for layer in layers:
        data = layer.data
        for command in data.get("deny_commands", []):
            deny_commands.setdefault(command, layer.name)

        if "only_commands" in data:
            only = set(data["only_commands"])
            for command in list(commands):
                if command not in only:
                    deny_commands.setdefault(command, layer.name)

        if layer.mode is not LayerMode.GRANT:
            for command, raw in (data.get("commands") or {}).items():
                rule = commands.get(command)
                if rule is None:
                    continue  # restrictions on commands nobody granted are no-ops
                raw = raw or {}
                updates: dict[str, Any] = {}
                if "subcommands" in raw:
                    restricted = frozenset(raw["subcommands"])
                    updates["subcommands"] = restricted if rule.subcommands is None else rule.subcommands & restricted
                if "deny_flags" in raw:
                    updates["deny_flags"] = {**{f: layer.name for f in raw["deny_flags"]}, **rule.deny_flags}
                if "prefix_args" in raw:
                    updates["prefix_args"] = rule.prefix_args + tuple(raw["prefix_args"])
                if "subcommand_args" in raw:
                    merged = dict(rule.subcommand_args)
                    for sub, args in raw["subcommand_args"].items():
                        merged[sub] = merged.get(sub, ()) + tuple(args)
                    updates["subcommand_args"] = merged
                if "env" in raw:
                    updates["env"] = {**rule.env, **raw["env"]}  # enforced env wins
                commands[command] = replace(rule, **updates)

        for tool, raw in (data.get("tools") or {}).items():
            raw = raw or {}
            current = tools.get(tool, ToolRule())
            enabled = current.enabled and raw.get("enabled", True)
            disabled_by = current.disabled_by or (layer.name if raw.get("enabled") is False else None)
            args = dict(current.args)
            for arg, arg_raw in (raw.get("args") or {}).items():
                arg_raw = arg_raw or {}
                prev = args.get(arg, ArgRule())
                one_of = prev.one_of
                if "one_of" in arg_raw:
                    new = frozenset(arg_raw["one_of"])
                    one_of = new if one_of is None else one_of & new
                patterns = prev.patterns + ((arg_raw["pattern"],) if "pattern" in arg_raw else ())
                max_length = prev.max_length
                if "max_length" in arg_raw:
                    max_length = arg_raw["max_length"] if max_length is None else min(max_length, arg_raw["max_length"])
                args[arg] = ArgRule(one_of=one_of, patterns=patterns, max_length=max_length)
            tools[tool] = ToolRule(
                enabled=enabled,
                disabled_by=disabled_by,
                args=args,
                layers=current.layers + (layer.name,),
            )

        limits = limits.capped(data.get("limits") or {})
        for pattern in (data.get("paths") or {}).get("deny_globs", []):
            if pattern not in deny_globs:
                deny_globs.append(pattern)

    frozen_commands = {
        name: replace(
            rule,
            deny_flags=MappingProxyType(dict(rule.deny_flags)),
            subcommand_args=MappingProxyType(dict(rule.subcommand_args)),
            env=MappingProxyType(dict(rule.env)),
        )
        for name, rule in commands.items()
    }
    return Policy(
        commands=MappingProxyType(frozen_commands),
        deny_commands=MappingProxyType(deny_commands),
        deny_globs=tuple(deny_globs),
        tools=MappingProxyType(tools),
        limits=limits,
        layers=tuple(layer.name for layer in layers),
        hash=_layer_hash(layers),
    )


# --- hot reload ------------------------------------------------------------------------------


class PolicySource:
    """Loads policy files and reloads them when they change.

    If a reload fails, every later call raises ``PolicyError`` until the files are fixed. The old
    policy is not kept: it may be more permissive than what the author intended (fail closed).
    """

    def __init__(self, layers: Sequence[tuple[str, Path]]) -> None:
        self._layers = [(name, Path(path)) for name, path in layers]
        self._stamp: tuple[Any, ...] | None = None
        self._policy: Policy | None = None
        self._error: PolicyError | None = None
        self.get()  # raises PolicyError at startup if the policy is invalid

    def _current_stamp(self) -> tuple[Any, ...]:
        stamp = []
        for _, path in self._layers:
            try:
                st = path.stat()
                stamp.append((st.st_mtime_ns, st.st_size, st.st_ino))
            except OSError:
                stamp.append(None)
        return tuple(stamp)

    def get(self) -> Policy:
        stamp = self._current_stamp()
        if stamp != self._stamp:
            self._stamp = stamp
            try:
                self._policy = merge([load_layer(path, name) for name, path in self._layers])
                self._error = None
            except PolicyError as exc:
                self._policy = None
                self._error = exc
        if self._error is not None or self._policy is None:
            raise self._error or PolicyError("policy is not loaded")
        return self._policy
