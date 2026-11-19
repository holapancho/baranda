import contextlib
import os
import stat
import subprocess
from pathlib import Path

import pytest

from baranda.guard import ErrorKind, Guard, ToolError

from .conftest import git, write

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


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


# $GIT_DIR/info/attributes is read whatever GIT_ATTR_SOURCE says, so it can still select drivers.
# Each case: driver config, attributes line, guarded argv, whether src/app.py is left modified.
INFO_ATTRIBUTE_ATTACKS = {
    "filter-clean-diff": ("filter.evil.clean", "* filter=evil\n", ["git", "diff"], True),
    "filter-clean-status": ("filter.evil.clean", "* filter=evil\n", ["git", "status"], True),
    "filter-process-diff": ("filter.evil.process", "* filter=evil\n", ["git", "diff"], True),
    "filter-macro-diff": ("filter.evil.clean", "[attr]innocent filter=evil\n* innocent\n", ["git", "diff"], True),
    "textconv-log": ("diff.evil.textconv", "*.py diff=evil\n", ["git", "log", "-p"], False),
    "textconv-show": ("diff.evil.textconv", "*.py diff=evil\n", ["git", "show"], False),
    "textconv-diff": ("diff.evil.textconv", "*.py diff=evil\n", ["git", "diff", "HEAD~1"], False),
    "textconv-blame": ("diff.evil.textconv", "*.py diff=evil\n", ["git", "blame", "src/app.py"], False),
    "command-diff": ("diff.evil.command", "*.py diff=evil\n", ["git", "diff", "HEAD~1"], False),
    # A subsection name with '=' and '.' cannot be overridden with -c (it splits at the first '=').
    "filter-odd-name-diff": ("filter.e=v.il.clean", "* filter=e=v.il\n", ["git", "diff"], True),
}


@pytest.mark.parametrize("case", INFO_ATTRIBUTE_ATTACKS)
def test_info_attributes_cannot_run_programs(repo, policy_dir, tmp_path, case):
    key, attributes, argv, modify = INFO_ATTRIBUTE_ATTACKS[case]
    marker = tmp_path / "pwned"
    script = _evil_script(tmp_path, marker)

    git(repo, "config", key, str(script))
    write(repo / ".git" / "info" / "attributes", attributes)
    if modify:
        write(repo / "src" / "app.py", "print('changed')\n")
        os.utime(repo / "src" / "app.py", (1, 1))

    # Control: plain git with the guard's own attribute settings still runs the program.
    marker.unlink(missing_ok=True)
    with contextlib.suppress(subprocess.CalledProcessError):  # the program ran; git may still fail
        git(repo, "-c", "core.attributesFile=/dev/null", *argv[1:], env={"GIT_ATTR_SOURCE": EMPTY_TREE})
    assert marker.exists(), f"control failed: plain git did not run the {key} program"
    marker.unlink()

    guard = Guard(repo, [policy_dir / "connector.yaml"])
    try:
        guard.run(argv)
    except ToolError:
        pass  # failing is fine; running the script is not
    assert not marker.exists(), f"{key} from info/attributes ran a program"


def test_info_attributes_drivers_are_blanked_not_broken(repo, guard, tmp_path):
    """Read-only commands still work on a repository that configures drivers."""
    marker = tmp_path / "pwned"
    script = _evil_script(tmp_path, marker)
    git(repo, "config", "filter.evil.clean", str(script))
    git(repo, "config", "filter.evil.required", "true")
    git(repo, "config", "diff.evil.textconv", str(script))
    write(repo / ".git" / "info" / "attributes", "* filter=evil\n*.py diff=evil\n")
    write(repo / "src" / "app.py", "print('changed')\n")
    os.utime(repo / "src" / "app.py", (1, 1))
    marker.unlink(missing_ok=True)

    assert "+print('changed')" in guard.run(["git", "diff"])
    assert "src/app.py" in guard.run(["git", "status", "--short"])
    assert "+print('hello world')" in guard.run(["git", "log", "-p", "-1"])
    assert "print('hello world')" in guard.run(["git", "blame", "HEAD", "--", "src/app.py"])
    assert not marker.exists()


@pytest.mark.parametrize(
    "argv", [["git", "status"], ["git", "diff"], ["git", "diff-files"], ["git", "diff-index", "HEAD"]]
)
def test_submodule_info_attributes_cannot_run_programs(repo, policy_dir, tmp_path, argv):
    """A submodule's own config and info/attributes are out of reach of the superproject's listing."""
    marker = tmp_path / "pwned"
    script = _evil_script(tmp_path, marker)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "main")
    write(upstream / "data.txt", "a\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-q", "-m", "data")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(upstream), "sub")
    git(repo, "commit", "-q", "-m", "submodule")

    sub = repo / "sub"
    git(sub, "config", "filter.evil.clean", str(script))
    gitdir = git(sub, "rev-parse", "--absolute-git-dir").strip()
    write(Path(gitdir) / "info" / "attributes", "* filter=evil\n")
    # It also tries to undo the guard's submodule settings from config and .gitmodules.
    git(repo, "config", "submodule.sub.ignore", "none")
    git(repo, "config", "-f", ".gitmodules", "submodule.sub.ignore", "none")
    git(repo, "config", "diff.submodule", "diff")
    write(sub / "data.txt", "b\n")  # same size, old mtime: git must hash it, through the filter
    os.utime(sub / "data.txt", (1, 1))

    marker.unlink(missing_ok=True)
    with contextlib.suppress(subprocess.CalledProcessError):  # the program ran; git may still fail
        git(repo, "-c", "core.attributesFile=/dev/null", *argv[1:], env={"GIT_ATTR_SOURCE": EMPTY_TREE})
    assert marker.exists(), "control failed: plain git did not run the submodule's filter"
    marker.unlink()

    write(policy_dir / "plumbing.yaml", "mode: grant\ncommands:\n  git:\n    subcommands: [status, diff, diff-files, diff-index]\n")
    guard = Guard(repo, [policy_dir / "plumbing.yaml"])
    try:
        guard.run(argv)
    except ToolError:
        pass
    assert not marker.exists(), "the submodule's filter ran a program"


@pytest.mark.parametrize("flag", ["--submodule=diff", "--ignore-submodules=none", "--recurse-submodules"])
def test_submodule_recursion_flags_are_denied(guard, flag):
    with pytest.raises(ToolError) as info:
        guard.run(["git", "diff", flag])
    assert info.value.kind is ErrorKind.DENIED
