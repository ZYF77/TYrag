"""Minimal in-process quality evaluation metrics."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any


class QualityMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)
        self._durations: list[float] = []

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._durations.clear()

    def inc(self, name: str, labels: dict[str, str] | None = None, value: int = 1) -> None:
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            self._counters[key] += value

    def observe_duration(self, name: str, seconds: float) -> None:
        with self._lock:
            self._durations.append((name, seconds))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters: dict[str, int] = defaultdict(int)
            for (name, _labels), value in self._counters.items():
                counters[name] += value
            duration_names = [name for name, _ in self._durations]
            return {
                "counters": dict(counters),
                "duration_samples": len(duration_names),
                "duration_total_seconds": round(
                    sum(seconds for _, seconds in self._durations), 3
                ),
            }


metrics = QualityMetrics()
