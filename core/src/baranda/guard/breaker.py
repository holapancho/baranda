"""Circuit breaker: stop a model from calling the same failing tool in a loop."""

from __future__ import annotations

import threading
import time
from typing import Callable, Hashable

from .errors import ErrorKind, ToolError


class CircuitBreaker:
    def __init__(
        self,
        threshold: int = 5,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[Hashable, int] = {}
        self._opened_at: dict[Hashable, float] = {}

    def check(self, key: Hashable) -> None:
        with self._lock:
            opened_at = self._opened_at.get(key)
            if opened_at is None:
                return
            remaining = self.cooldown_seconds - (self._clock() - opened_at)
            if remaining > 0:
                raise ToolError(
                    ErrorKind.CIRCUIT_OPEN,
                    f"this tool failed {self._failures.get(key, 0)} times in a row; "
                    f"try a different approach or wait {remaining:.0f}s",
                )

    def success(self, key: Hashable) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._opened_at.pop(key, None)

    def failure(self, key: Hashable) -> None:
        with self._lock:
            count = self._failures.get(key, 0) + 1
            self._failures[key] = count
            if count >= self.threshold:
                self._opened_at[key] = self._clock()
