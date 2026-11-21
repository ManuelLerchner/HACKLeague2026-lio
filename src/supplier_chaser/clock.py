from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    def __init__(self, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("Clock requires timezone-aware time")
        self.value = now.astimezone(UTC)

    def now(self) -> datetime:
        return self.value
