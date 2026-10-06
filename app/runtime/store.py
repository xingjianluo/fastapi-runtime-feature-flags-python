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
    """读写均基于不可变快照；刷新失败保留最近有效配置。

    读路径不加锁：快照是不可变对象，引用赋值原子，读者要么拿到旧的完整
    快照、要么拿到新的完整快照，绝不会看到新旧混杂。
    """

    def __init__(self, initial: Snapshot) -> None:
        self._lock = threading.RLock()
        self._snapshot = initial
        self._history: list[Snapshot] = [initial]
        self._audit: list[AuditRecord] = []

    def current(self) -> Snapshot:
        """返回当前生效快照（不可变对象，读路径无锁）。"""
        return self._snapshot

    def refresh(
        self,
        config: dict[str, Any] | None,
        flags: FlagSet,
        report: RejectionReport,
        actor: str = "system",
    ) -> RefreshResult:
        """整体替换当前快照；存在致命拒绝时不做任何变更。"""
        with self._lock:
            if config is None or report.has_fatal:
                detail = f"刷新被拒绝：{len(report.rejections)} 条拒绝记录"
                self._audit.append(AuditRecord(
                    version=self._snapshot.version, action="refresh_rejected",
                    actor=actor, detail=detail,
                ))
                return RefreshResult(ok=False, version=self._snapshot.version,
                                     report=report, detail=detail)
            snapshot = Snapshot(version=self._snapshot.version + 1, config=config, flags=flags)
            self._snapshot = snapshot
            self._history.append(snapshot)
            self._audit.append(AuditRecord(
                version=snapshot.version, action="refresh", actor=actor,
                detail=f"配置项 {len(config)} 个，开关 {len(flags.flags)} 个",
            ))
            return RefreshResult(ok=True, version=snapshot.version, report=report,
                                 detail="刷新成功")

    def rollback(self, to_version: int, actor: str = "system") -> RefreshResult:
        """回滚到历史版本；目标版本不存在时不做任何变更。"""
        with self._lock:
            target = next((s for s in self._history if s.version == to_version), None)
            if target is None:
                detail = f"回滚被拒绝：版本 {to_version} 不在历史中"
                self._audit.append(AuditRecord(
                    version=self._snapshot.version, action="rollback_rejected",
                    actor=actor, detail=detail,
                ))
                return RefreshResult(ok=False, version=self._snapshot.version, detail=detail)
            self._snapshot = target
            self._audit.append(AuditRecord(
                version=target.version, action="rollback", actor=actor,
                detail=f"回滚到版本 {to_version}",
            ))
            return RefreshResult(ok=True, version=target.version,
                                 detail=f"已回滚到版本 {to_version}")

    def audit_log(self) -> list[AuditRecord]:
        """返回审计记录副本（按发生顺序）。"""
        with self._lock:
            return list(self._audit)
