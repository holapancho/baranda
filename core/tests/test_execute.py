import os
import stat

import pytest

from baranda.guard import ErrorKind, Guard, ToolError

from .conftest import git, write


def test_git_log_runs(guard):
    out = guard.run(["git", "log", "--format=%s"])
    assert out.splitlines() == ["say hello world", "initial commit"]


def test_rg_no_match_is_not_an_error(guard):
    assert guard.run(["rg", "does-not-exist-anywhere"]) == ""


def test_failing_command_is_invalid_input_with_stderr(guard):
    with pytest.raises(ToolError) as info:
        guard.run(["git", "show", "no-such-revision"])
    assert info.value.kind is ErrorKind.INVALID_INPUT
    assert "exited with code" in info.value.message


def test_environment_is_clean(guard, monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "leak-me")
    out = guard.run(["printenv"])
    assert "leak-me" not in out
    assert "HOME=/nonexistent" in out


def test_timeout(repo, policy_dir):
    write(policy_dir / "user.yaml", "mode: restrict\nlimits:\n  timeout_seconds: 0.5\n")
    guard = Guard(repo, [policy_dir / "connector.yaml", policy_dir / "user.yaml"])
    with pytest.raises(ToolError) as info:
        guard.run(["sleep", "5"])
    assert info.value.kind is ErrorKind.UNAVAILABLE
    assert "timed out" in info.value.message


def test_output_is_capped(repo, policy_dir):
    write(policy_dir / "user.yaml", "mode: restrict\nlimits:\n  max_output_bytes: 4096\n  timeout_seconds: 5\n")
    guard = Guard(repo, [policy_dir / "connector.yaml", policy_dir / "user.yaml"])
    result = guard.execute(["yes"])
    assert result.truncated
    assert result.stdout.endswith("[output truncated]")
    assert len(result.stdout) <= 4096 + 50


# --- a malicious repository cannot make git run programs -------------------------------------


def _evil_script(tmp_path, marker):
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.mark.parametrize(
    ("config", "argv", "setup", "works_without_guard"),
    [
        ("core.fsmonitor", ["git", "status"], None, True),
        # git only starts a pager on a terminal, so plain git does not run it here either.
        ("core.pager", ["git", "log"], None, False),
        ("diff.external", ["git", "diff"], "modify", True),
        ("filter", ["git", "diff"], "filter", True),
        ("textconv", ["git", "log", "-p"], "textconv", True),
    ],
)
def test_repo_config_cannot_run_programs(repo, policy_dir, tmp_path, config, argv, setup, works_without_guard):
    marker = tmp_path / "pwned"
    script = _evil_script(tmp_path, marker)

    if config == "filter":
        git(repo, "config", "filter.evil.clean", str(script))
        write(repo / ".gitattributes", "* filter=evil\n")
        git(repo, "add", ".gitattributes")
        git(repo, "commit", "-q", "-m", "attributes")
    elif config == "textconv":
        git(repo, "config", "diff.evil.textconv", str(script))
        write(repo / ".gitattributes", "*.py diff=evil\n")
        git(repo, "add", ".gitattributes")
        git(repo, "commit", "-q", "-m", "attributes")
    else:
        git(repo, "config", config, str(script))
    if setup in ("modify", "filter"):
        write(repo / "src" / "app.py", "print('changed')\n")
        os.utime(repo / "src" / "app.py", (1, 1))

    # Control: prove the attack works with plain git, so the guarded run below means something.
    marker.unlink(missing_ok=True)  # setup itself (git add) may have triggered it
    if works_without_guard:
        try:
            git(repo, *argv[1:])
        except Exception:
            pass
        assert marker.exists(), f"control failed: plain git did not run the {config} program"
        marker.unlink()

    guard = Guard(repo, [policy_dir / "connector.yaml"])
    try:
        guard.run(argv)
    except ToolError:
        pass  # failing is fine; running the script is not
    assert not marker.exists(), f"{config} ran a program"
