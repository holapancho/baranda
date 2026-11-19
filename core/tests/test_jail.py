import os

import pytest

from baranda.guard import Denied, Jail
from baranda.guard.jail import matches_any


def test_paths_inside_resolve(repo):
    jail = Jail(repo)
    assert jail.resolve("src/app.py") == repo.resolve() / "src" / "app.py"
    assert jail.resolve(".") == repo.resolve()
    assert jail.resolve(str(repo / "README.md")) == repo.resolve() / "README.md"


@pytest.mark.parametrize("path", ["..", "../x", "src/../../x", "/etc/passwd", "/"])
def test_escapes_are_denied(repo, path):
    with pytest.raises(Denied):
        Jail(repo).resolve(path)


def test_symlink_escape_is_denied(repo, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    os.symlink(outside, repo / "link")
    with pytest.raises(Denied):
        Jail(repo).resolve("link")


def test_symlink_inside_is_allowed(repo):
    os.symlink(repo / "README.md", repo / "readme-link")
    assert Jail(repo).resolve("readme-link") == repo.resolve() / "README.md"


def test_nul_byte_is_denied(repo):
    with pytest.raises(Denied):
        Jail(repo).resolve("README.md\x00")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".env", ".env"),
        ("config/.env", ".env"),
        (".env.local", ".env.*"),
        ("keys/server.pem", "*.pem"),
        ("src/app.py", None),
        ("deep/nested/secrets.json", "**/secrets.json"),
        ("secrets.json", "**/secrets.json"),
    ],
)
def test_deny_globs(path, expected):
    patterns = (".env", ".env.*", "*.pem", "**/secrets.json")
    assert matches_any(path, patterns) == expected
