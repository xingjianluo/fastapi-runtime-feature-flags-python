"""运行时配置存储：原子快照、在线刷新、回滚与审计。"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from app.config.errors import RejectionReport
from app.flags.model import FlagSet


@dataclass(frozen=True)
class AuditRecord:
    """一条变更审计记录（敏感值已脱敏）。"""
    version: int
    action: str  # "refresh" | "rollback" | "refresh_rejected"
    actor: str
    detail: str = ""


@dataclass(frozen=True)
class Snapshot:
    """一份完整配置快照：业务配置 + 开关集，整体原子替换。"""
    version: int
    config: dict[str, Any]
    flags: FlagSet


@dataclass
class RefreshResult:
    ok: bool
    version: int
    report: RejectionReport = field(default_factory=RejectionReport)
    detail: str = ""


class ConfigStore:
    """读写均基于不可变快照；刷新失败保留最近有效配置。"""

    def __init__(self, initial: Snapshot) -> None:
        self._lock = threading.RLock()
        self._snapshot = initial
        self._history: list[Snapshot] = [initial]
        self._audit: list[AuditRecord] = []

    def current(self) -> Snapshot:
        raise NotImplementedError

    def refresh(
        self,
        config: dict[str, Any] | None,
        flags: FlagSet,
        report: RejectionReport,
        actor: str = "system",
    ) -> RefreshResult:
        raise NotImplementedError

    def rollback(self, to_version: int, actor: str = "system") -> RefreshResult:
        raise NotImplementedError

    def audit_log(self) -> list[AuditRecord]:
        raise NotImplementedError
