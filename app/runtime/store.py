"""运行时配置存储：原子快照、在线刷新、回滚与审计。

- 读写均基于不可变快照：读者要么看到旧的完整一套，要么看到新的完整一套；
- 刷新前先做组合关系校验，任何一条不合法即整体拒绝，继续沿用上一份有效配置；
- 临时覆盖由 OverrideManager 持有，优先级高于规则判定。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from app.config.errors import RejectionReport
from app.flags.evaluator import Decision, evaluate
from app.flags.model import FlagSet
from app.flags.overrides import OverrideManager
from app.flags.relations import validate_relations


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

    def __init__(self, initial: Snapshot, overrides: OverrideManager | None = None) -> None:
        self._lock = threading.RLock()
        self._snapshot = initial
        self._history: list[Snapshot] = [initial]
        self._audit: list[AuditRecord] = []
        self.overrides = overrides

    def current(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def refresh(
        self,
        config: dict[str, Any] | None,
        flags: FlagSet,
        report: RejectionReport | None = None,
        actor: str = "system",
    ) -> RefreshResult:
        """整体替换为候选配置；校验不过则拒绝且线上状态不变。

        config 为 None 表示沿用当前业务配置，只更新开关集。
        """
        merged_report = RejectionReport()
        if report is not None:
            merged_report.rejections.extend(report.rejections)
        merged_report.rejections.extend(validate_relations(flags).rejections)
        with self._lock:
            if not merged_report.ok:
                self._audit.append(AuditRecord(
                    self._snapshot.version, "refresh_rejected", actor,
                    f"{len(merged_report.rejections)} 条校验未通过，沿用 v{self._snapshot.version}",
                ))
                return RefreshResult(False, self._snapshot.version, merged_report,
                                     "校验未通过，本次发布被拒绝")
            new_snapshot = Snapshot(
                version=self._snapshot.version + 1,
                config=self._snapshot.config if config is None else config,
                flags=flags,
            )
            self._snapshot = new_snapshot
            self._history.append(new_snapshot)
            self._audit.append(AuditRecord(
                new_snapshot.version, "refresh", actor,
                f"flags={len(flags.flags)} config_keys={len(new_snapshot.config)}",
            ))
            return RefreshResult(True, new_snapshot.version, merged_report, "已生效")

    def rollback(self, to_version: int, actor: str = "system") -> RefreshResult:
        """回滚到历史版本；目标版本不存在时拒绝且状态不变。"""
        with self._lock:
            target = next((s for s in self._history if s.version == to_version), None)
            if target is None:
                return RefreshResult(False, self._snapshot.version,
                                     detail=f"历史版本 v{to_version} 不存在")
            self._snapshot = target
            self._audit.append(AuditRecord(
                target.version, "rollback", actor, f"回滚到 v{to_version}",
            ))
            return RefreshResult(True, target.version, detail=f"已回滚到 v{to_version}")

    def evaluate_flag(self, name: str, context: dict[str, Any]) -> Decision:
        """对当前快照求值；快照在调用点一次性取出，请求内不会新旧混杂。"""
        snapshot = self.current()
        return evaluate(snapshot.flags, name, context, overrides=self.overrides)

    def audit_log(self) -> list[AuditRecord]:
        with self._lock:
            return list(self._audit)
