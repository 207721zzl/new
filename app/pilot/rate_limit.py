"""适用于单 API 进程试点的内存滑动窗口限流。"""

from collections import defaultdict, deque
from math import ceil
from threading import Lock
from time import monotonic


class PilotRateLimiter:
    """不保存原始 Cookie，只根据调用方提供的匿名键计数。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def check(
        self,
        *,
        scope: str,
        client_key: str,
        limit: int,
        window_seconds: int,
        now: float | None = None,
    ) -> int | None:
        current = monotonic() if now is None else now
        cutoff = current - window_seconds
        key = (scope, client_key)
        with self._lock:
            bucket = self._buckets[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return max(1, ceil(window_seconds - (current - bucket[0])))
            bucket.append(current)
            if len(self._buckets) > 10_000:
                self._discard_empty(cutoff)
        return None

    def _discard_empty(self, cutoff: float) -> None:
        for key in list(self._buckets):
            bucket = self._buckets[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if not bucket:
                del self._buckets[key]

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


pilot_rate_limiter = PilotRateLimiter()
