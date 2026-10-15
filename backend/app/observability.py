"""Small dependency-free runtime guards and metrics for the API process."""
import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass


class SlidingWindowRateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        async with self._lock:
            events = self._events[key]
            cutoff = now - self.window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                return False, max(1, int(events[0] + self.window_seconds - now))
            events.append(now)
            return True, 0


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    recovery_seconds: int = 30
    failures: int = 0
    opened_at: float | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= self.recovery_seconds:
            self.opened_at = None
            self.failures = 0
            return True
        return False

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = time.monotonic()


class RuntimeMetrics:
    def __init__(self):
        self.requests = defaultdict(int)
        self.latency_seconds = defaultdict(float)
        self.in_flight = 0

    def request_started(self) -> None:
        self.in_flight += 1

    def request_finished(self, method: str, path: str, status: int, elapsed: float) -> None:
        self.in_flight = max(0, self.in_flight - 1)
        key = (method, path, str(status))
        self.requests[key] += 1
        self.latency_seconds[key] += elapsed

    def prometheus(self) -> str:
        lines = [
            "# HELP api_requests_total Total HTTP requests.",
            "# TYPE api_requests_total counter",
        ]
        for (method, path, status), value in sorted(self.requests.items()):
            labels = f'method="{method}",path="{path}",status="{status}"'
            lines.append(f"api_requests_total{{{labels}}} {value}")
        lines.extend([
            "# HELP api_request_duration_seconds_sum Request duration sum.",
            "# TYPE api_request_duration_seconds_sum counter",
        ])
        for (method, path, status), value in sorted(self.latency_seconds.items()):
            labels = f'method="{method}",path="{path}",status="{status}"'
            lines.append(f"api_request_duration_seconds_sum{{{labels}}} {value:.6f}")
        lines.extend([
            "# HELP api_requests_in_flight Current requests in flight.",
            "# TYPE api_requests_in_flight gauge",
            f"api_requests_in_flight {self.in_flight}",
        ])
        return "\n".join(lines) + "\n"
