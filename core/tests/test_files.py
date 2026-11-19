import os

import pytest

from baranda.guard import Denied, ErrorKind, Guard, ToolError

from .conftest import write


def test_read_file(guard):
    assert guard.read("src/app.py") == "print('hello world')\n"


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", ".env", "config/server.pem"])
def test_read_denied(guard, repo, path):
    write(repo / ".env", "TOKEN=abc")
    write(repo / "config" / "server.pem", "key")
    with pytest.raises(Denied):
        guard.read(path)


def test_read_through_symlink_out_of_the_workspace_is_denied(guard, repo, tmp_path):
    outside = write(tmp_path / "secret.txt", "secret")
    os.symlink(outside, repo / "innocent.txt")
    with pytest.raises(Denied):
        guard.read("innocent.txt")


def test_read_errors(guard):
    for path, text in (("missing.txt", "not found"), ("src", "not a regular file")):
        with pytest.raises(ToolError) as info:
            guard.read(path)
        assert info.value.kind is ErrorKind.INVALID_INPUT
        assert text in info.value.message


def test_read_is_capped(repo, policy_dir):
    write(repo / "big.txt", "x" * 10_000)
    write(policy_dir / "user.yaml", "mode: restrict\nlimits:\n  max_read_bytes: 100\n")
    guard = Guard(repo, [policy_dir / "connector.yaml", policy_dir / "user.yaml"])
    text = guard.read("big.txt")
    assert text.startswith("x" * 100)
    assert text.endswith("[output truncated]")


def test_list_dir(guard):
    assert guard.list_dir(".") == [".git/", "README.md", "src/"]
    assert guard.list_dir("src") == ["app.py"]


def test_list_dir_outside_is_denied(guard):
    with pytest.raises(Denied):
        guard.list_dir("..")
