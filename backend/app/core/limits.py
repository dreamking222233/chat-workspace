from collections import defaultdict, deque
from threading import Lock
from time import monotonic


class SlidingWindowLimiter:
    """Small process-local limiter for development and single-instance deploys."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = monotonic()
        cutoff = now - max(1, window_seconds)
        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= max(1, limit):
                retry_after = max(1, int(bucket[0] + max(1, window_seconds) - now) + 1)
                return False, retry_after
            bucket.append(now)
            return True, 0


limiter = SlidingWindowLimiter()
