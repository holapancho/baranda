"""Path jail: every path a tool touches must resolve inside the workspace."""

from __future__ import annotations

import os
from fnmatch import fnmatchcase
from pathlib import Path

from .errors import Denied


class Jail:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        resolved = Path(root).resolve(strict=True)
        if not resolved.is_dir():
            raise NotADirectoryError(f"workspace is not a directory: {resolved}")
        self.root = resolved

    def resolve(self, path: str | os.PathLike[str]) -> Path:
        """Resolve ``path`` (relative to the root) following symlinks; deny if it escapes."""
        text = os.fspath(path)
        if "\x00" in text:
            raise Denied("path contains a NUL byte")
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        real = Path(os.path.realpath(candidate))
        if real != self.root and self.root not in real.parents:
            raise Denied(f"path is outside the workspace: {text}")
        return real

    def relative(self, real: Path) -> str:
        """Workspace-relative POSIX path of an already resolved path ("." for the root)."""
        rel = real.relative_to(self.root).as_posix()
        return rel or "."


def matches_any(rel_path: str, patterns: tuple[str, ...]) -> str | None:
    """Return the first pattern that matches, gitignore-style.

    A pattern without ``/`` matches the file name at any depth (``*.pem``). A pattern with ``/``
    matches the whole workspace-relative path; a leading ``**/`` also matches at the root.
    """
    name = rel_path.rsplit("/", 1)[-1]
    for pattern in patterns:
        if "/" not in pattern:
            if fnmatchcase(name, pattern):
                return pattern
            continue
        if fnmatchcase(rel_path, pattern):
            return pattern
        if pattern.startswith("**/") and fnmatchcase(rel_path, pattern[3:]):
            return pattern
    return None
