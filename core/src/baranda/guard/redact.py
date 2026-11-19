"""Best-effort removal of secrets from text that leaves guard (errors, audit records)."""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        REDACTED,
    ),
    (re.compile(r"(?i)(authorization:\s*(?:bearer|basic|token)\s+)\S+"), r"\1" + REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\b00D[A-Za-z0-9]{12,15}![A-Za-z0-9._]{20,}"), REDACTED),
    (
        re.compile(r"(?i)\b((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)\S+"),
        r"\1" + REDACTED,
    ),
)


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [truncated {len(text) - limit} chars]"


def sanitize(text: str, limit: int = 2000) -> str:
    """Redact, then truncate. Use for anything untrusted that is shown to the model."""
    return truncate(redact(text), limit)
