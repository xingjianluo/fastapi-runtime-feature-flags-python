"""带有效期的临时覆盖：强制开关开/关一段时间，到期自动恢复规则判定。

- 覆盖生效期间优先级高于规则求值结果；
- 到期判断基于可注入时钟，本地与单测可确定性验证；
- 到期采用惰性清理：读覆盖时发现已过期即记录 expired 事件并移除，
  不依赖后台线程，行为完全由时钟驱动、可重放；
- 生效、到期、撤销均记录事件，对外展示时敏感开关的值脱敏。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

MASK = "***"


@dataclass(frozen=True)
class Override:
    """一条生效中的临时覆盖。"""
    flag: str
    value: Any
    created_at: float
    expires_at: float
    actor: str
    reason: str = ""


@dataclass(frozen=True)
class OverrideEvent:
    """覆盖生命周期事件：set / expired / revoked。"""
    flag: str
    action: str
    actor: str
    at: float
    value: Any = None
    detail: str = ""


class OverrideManager:
    """覆盖的存取与生命周期管理；时钟可注入，线程安全。"""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._overrides: dict[str, Override] = {}
        self._events: list[OverrideEvent] = []

    def set(self, flag: str, value: Any, ttl_seconds: float,
            actor: str = "system", reason: str = "") -> Override:
        """强制 flag 取 value，ttl_seconds 后自动到期；同 flag 重复设置视为替换。"""
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds 必须为正数")
        with self._lock:
            now = self._clock()
            override = Override(flag=flag, value=value, created_at=now,
                                expires_at=now + ttl_seconds, actor=actor, reason=reason)
            replaced = flag in self._overrides
            self._overrides[flag] = override
            self._events.append(OverrideEvent(
                flag=flag, action="set", actor=actor, at=now, value=value,
                detail=("替换已有覆盖；" if replaced else "") + f"ttl={ttl_seconds}s {reason}".strip(),
            ))
            return override

    def _purge_expired_locked(self) -> None:
        now = self._clock()
        expired = [name for name, o in self._overrides.items() if now >= o.expires_at]
        for name in sorted(expired):
            o = self._overrides.pop(name)
            self._events.append(OverrideEvent(
                flag=name, action="expired", actor=o.actor, at=o.expires_at,
                value=o.value, detail="覆盖到期，恢复规则判定",
            ))

    def get(self, flag: str) -> Override | None:
        """取 flag 的生效中覆盖；已过期则记录到期事件并返回 None。"""
        with self._lock:
            self._purge_expired_locked()
            return self._overrides.get(flag)

    def revoke(self, flag: str, actor: str = "system") -> bool:
        """提前撤销覆盖；返回是否真的撤销了生效中的覆盖。"""
        with self._lock:
            self._purge_expired_locked()
            o = self._overrides.pop(flag, None)
            if o is None:
                return False
            self._events.append(OverrideEvent(
                flag=flag, action="revoked", actor=actor, at=self._clock(),
                value=o.value, detail="覆盖被提前撤销",
            ))
            return True

    def active(self) -> list[Override]:
        """当前生效中的全部覆盖（按开关名排序，先清理过期项）。"""
        with self._lock:
            self._purge_expired_locked()
            return [self._overrides[k] for k in sorted(self._overrides)]

    def events(self) -> list[OverrideEvent]:
        """全部生命周期事件（按发生顺序）；读取前先做惰性到期清理。"""
        with self._lock:
            self._purge_expired_locked()
            return list(self._events)


def mask_events(events: list[OverrideEvent],
                sensitive_flags: set[str] | None = None) -> list[dict[str, Any]]:
    """对外展示用：敏感开关的覆盖值脱敏。"""
    sensitive = sensitive_flags or set()
    return [
        {
            "flag": e.flag,
            "action": e.action,
            "actor": e.actor,
            "at": e.at,
            "value": MASK if e.flag in sensitive and e.value is not None else e.value,
            "detail": e.detail,
        }
        for e in events
    ]
