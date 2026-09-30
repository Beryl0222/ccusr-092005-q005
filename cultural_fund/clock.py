"""可注入的时钟，保证规则生效、锁定时点等在测试中可确定。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def today(self) -> str: ...


class SystemClock:
    """真实墙钟，统一使用 UTC 并以带时区的 ISO8601 落库。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def today(self) -> str:
        return self.now().date().isoformat()


class VirtualClock:
    """测试/回放用固定时钟，可手动推进。"""

    def __init__(self, now: datetime | str) -> None:
        if isinstance(now, str):
            now = datetime.fromisoformat(now)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        self._now = now

    def now(self) -> datetime:
        return self._now

    def today(self) -> str:
        return self._now.date().isoformat()

    def advance(self, **kwargs: int) -> None:
        self._now = self._now + timedelta(**kwargs)

    def set(self, now: datetime | str) -> None:
        if isinstance(now, str):
            now = datetime.fromisoformat(now)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        self._now = now
