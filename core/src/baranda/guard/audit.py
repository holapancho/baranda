"""Append-only audit log with chained hashes.

Each record stores the hash of the previous one, so editing or deleting a line breaks the chain and
``verify`` reports where. The audit stores decisions, never command outputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _canonical(record: dict[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def _digest(record: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(record)).hexdigest()


class AuditError(Exception):
    pass


class AuditLog:
    """Writes JSONL records to ``path``. With ``path=None`` records are only kept in memory."""

    def __init__(self, path: str | os.PathLike[str] | None = None, *, keep: int = 1000) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._last = GENESIS
        self._keep = keep
        self.records: list[dict[str, Any]] = []
        if self.path is not None and self.path.exists():
            last_line = ""
            with self.path.open(encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        last_line = line
            if last_line:
                self._last = json.loads(last_line)["hash"]

    def record(self, **fields: Any) -> dict[str, Any]:
        with self._lock:
            record = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                **fields,
                "prev": self._last,
            }
            record["hash"] = _digest(record)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(_canonical(record).decode() + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            self._last = record["hash"]
            self.records.append(record)
            if len(self.records) > self._keep:
                del self.records[: len(self.records) - self._keep]
            return record


def verify(path: str | os.PathLike[str]) -> int:
    """Check the whole chain. Returns the number of records; raises ``AuditError`` on tampering."""
    previous = GENESIS
    count = 0
    with Path(path).open(encoding="utf-8") as fh:
        for number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                raise AuditError(f"line {number}: not valid JSON") from None
            stored = record.pop("hash", None)
            if record.get("prev") != previous:
                raise AuditError(f"line {number}: chain broken (prev does not match)")
            if stored != _digest(record):
                raise AuditError(f"line {number}: hash mismatch (record was modified)")
            previous = stored
            count += 1
    return count
