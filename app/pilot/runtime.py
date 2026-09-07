"""不采集请求正文的轻量进程内 HTTP 指标。"""

from collections import deque
from datetime import UTC, datetime
from math import ceil
from threading import Lock
from time import monotonic


class PilotRuntimeMetrics:
    """为单进程试点提供低开销指标；进程重启后按预期重新计数。"""

    def __init__(self, sample_size: int = 2000) -> None:
        self._lock = Lock()
        self._started_monotonic = monotonic()
        self._started_at = datetime.now(UTC)
        self._requests_total = 0
        self._responses_4xx = 0
        self._responses_5xx = 0
        self._durations_ms: deque[float] = deque(maxlen=sample_size)

    def record(self, *, status_code: int, duration_ms: float) -> None:
        with self._lock:
            self._requests_total += 1
            if 400 <= status_code < 500:
                self._responses_4xx += 1
            elif status_code >= 500:
                self._responses_5xx += 1
            self._durations_ms.append(max(0.0, float(duration_ms)))

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        rank = max(1, ceil(percentile * len(ordered)))
        return round(ordered[rank - 1], 2)

    def snapshot(self) -> dict[str, int | float | str | None]:
        with self._lock:
            durations = list(self._durations_ms)
            return {
                "started_at": self._started_at.isoformat(),
                "uptime_seconds": round(monotonic() - self._started_monotonic, 2),
                "requests_total": self._requests_total,
                "responses_4xx": self._responses_4xx,
                "responses_5xx": self._responses_5xx,
                "latency_sample_count": len(durations),
                "average_latency_ms": (
                    round(sum(durations) / len(durations), 2) if durations else None
                ),
                "p95_latency_ms": self._percentile(durations, 0.95),
            }

    def reset(self) -> None:
        """仅供测试隔离。"""
        with self._lock:
            self._started_monotonic = monotonic()
            self._started_at = datetime.now(UTC)
            self._requests_total = 0
            self._responses_4xx = 0
            self._responses_5xx = 0
            self._durations_ms.clear()


pilot_runtime_metrics = PilotRuntimeMetrics()
