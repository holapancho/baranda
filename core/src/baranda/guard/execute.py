"""Run a prepared command: no shell, clean env, timeout and a hard output limit."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass

from .commands import PreparedCommand
from .errors import ErrorKind, ToolError

TRUNCATED_MARKER = "\n[output truncated]"


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    duration_ms: float


def _limit_output(max_bytes: int):  # pragma: no cover - runs in the child process
    def apply() -> None:
        import resource

        # The child's stdout and stderr are files, so this caps them. Writing past the limit
        # raises SIGXFSZ and the child dies instead of filling the disk.
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_bytes, max_bytes))

    return apply


def run(cmd: PreparedCommand) -> CommandResult:
    start = time.perf_counter()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(
                list(cmd.argv),
                cwd=cmd.cwd,
                env=dict(cmd.env),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                close_fds=True,
                start_new_session=True,
                preexec_fn=_limit_output(cmd.max_output_bytes),
            )
        except FileNotFoundError:
            raise ToolError(ErrorKind.UNAVAILABLE, f"command not available: {cmd.argv[0]}") from None

        try:
            exit_code = proc.wait(timeout=cmd.timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            raise ToolError(
                ErrorKind.UNAVAILABLE,
                f"command timed out after {cmd.timeout:g}s: {cmd.argv[0]}",
            ) from None

        out.seek(0)
        err.seek(0)
        stdout = out.read(cmd.max_output_bytes + 1)
        stderr = err.read(cmd.max_output_bytes + 1)

    truncated = exit_code == -signal.SIGXFSZ or len(stdout) >= cmd.max_output_bytes
    text = stdout[: cmd.max_output_bytes].decode("utf-8", errors="replace")
    if truncated:
        text += TRUNCATED_MARKER
    return CommandResult(
        argv=cmd.argv,
        exit_code=exit_code,
        stdout=text,
        stderr=stderr[: cmd.max_output_bytes].decode("utf-8", errors="replace"),
        truncated=truncated,
        duration_ms=(time.perf_counter() - start) * 1000,
    )
