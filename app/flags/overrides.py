"""带有效期的临时覆盖：强制开/关某个开关一段时间，到期自动失效。

时间通过注入的 clock 获取，本地与单测可确定性验证。
覆盖的生效、到期、撤销都会记入审计日志；敏感开关的值在对外展示时脱敏。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Any, Callable

from app.config.loader import MASK


@dataclass(frozen=True)
class Override:
    """一条临时覆盖：在 expires_at 之前强制 flag 取 value。"""
    flag: str
    value: Any
    expires_at: float   # epoch 秒
    created_at: float
    actor: str = "system"
    reason: str = ""


@dataclass(frozen=True)
class OverrideEvent:
    """一条覆盖审计事件：set / expired / revoked。"""
    action: str
    flag: str
    at: float
    actor: str = "system"
    value: Any = None   # set 时的强制取值（对外展示需按敏感标记脱敏）
    detail: str = ""


class OverrideManager:
    """覆盖的运行时状态；读写加锁，判定基于注入时钟。"""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._overrides: dict[str, Override] = {}
        self._events: list[OverrideEvent] = []
        # 已记录过 expired 事件的覆盖标识，避免并发读时重复记账
        self._expired_recorded: set[tuple[str, float, float]] = set()

    def set_override(self, override: Override) -> None:
        """设置（或替换）某开关的覆盖；同一开关同一时刻只有一条覆盖。"""
        with self._lock:
            self._overrides[override.flag] = override
            self._events.append(OverrideEvent(
                action="set", flag=override.flag, at=self._clock(),
                actor=override.actor, value=override.value,
                detail=f"expires_at={override.expires_at} reason={override.reason}",
            ))

    def revoke(self, flag: str, actor: str = "system") -> bool:
        """撤销覆盖；返回是否存在可撤销的覆盖。"""
        with self._lock:
            existing = self._overrides.pop(flag, None)
            if existing is None:
                return False
            self._events.append(OverrideEvent(
                action="revoked", flag=flag, at=self._clock(), actor=actor,
                detail=f"原覆盖值已撤销（原 expires_at={existing.expires_at}）",
            ))
            return True

    def active_for(self, flag: str) -> Override | None:
        """返回该开关当前生效中的覆盖；到期则记录 expired 事件并返回 None。"""
        now = self._clock()
        with self._lock:
            override = self._overrides.get(flag)
            if override is None:
                return None
            if now < override.expires_at:
                return override
            key = (flag, override.created_at, override.expires_at)
            if key not in self._expired_recorded:
                self._expired_recorded.add(key)
                self._events.append(OverrideEvent(
                    action="expired", flag=flag, at=now,
                    detail=f"覆盖已于 expires_at={override.expires_at} 到期，恢复规则判定",
                ))
            del self._overrides[flag]
            return None

    def events(self) -> list[OverrideEvent]:
        """全部审计事件（含敏感原值，仅供内部）。"""
        with self._lock:
            return list(self._events)

    def masked_events(self, sensitive_flags: set[str]) -> list[OverrideEvent]:
        """对外展示：敏感开关的取值脱敏。"""
        with self._lock:
            return [
                replace(e, value=MASK)
                if e.flag in sensitive_flags and e.value is not None else e
                for e in self._events
            ]
