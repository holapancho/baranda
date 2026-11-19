from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from baranda.guard import AuditLog, Guard

GRANT = """\
mode: grant
commands:
  git:
    subcommands: [log, diff, show, blame, status]
  rg:
    ok_exit_codes: [0, 1]
  jq: {}
  # test-only grants
  printenv: {}
  sleep: {}
  "yes": {}  # quoted: YAML reads a bare yes as true
"""

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root / "README.md", "# demo\n")
    write(root / "src" / "app.py", "print('hello')\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "initial commit")
    write(root / "src" / "app.py", "print('hello world')\n")
    git(root, "commit", "-q", "-am", "say hello world")
    return root


@pytest.fixture
def policy_dir(tmp_path: Path) -> Path:
    path = tmp_path / "policy"
    path.mkdir()
    write(path / "connector.yaml", GRANT)
    return path


@pytest.fixture
def audit() -> AuditLog:
    return AuditLog()


@pytest.fixture
def guard(repo: Path, policy_dir: Path, audit: AuditLog) -> Guard:
    return Guard(repo, [policy_dir / "connector.yaml"], audit=audit)
