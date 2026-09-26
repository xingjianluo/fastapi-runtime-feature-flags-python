"""审计记录：刷新/回滚事件可追溯，事件本身不包含任何敏感值。"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from threading import Lock


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    at: str
    action: str  # load | refresh | rollback | reject
    old_version: int | None
    new_version: int | None
    success: bool
    reason: str | None
    sources: tuple[str, ...]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sources"] = list(self.sources)
        return d


class AuditLog:
    """线程安全的追加式审计日志。"""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = Lock()

    def append(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def entries(self) -> list[AuditEvent]:
        with self._lock:
            return list(self._events)

    def next_seq(self) -> int:
        with self._lock:
            return len(self._events) + 1

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
