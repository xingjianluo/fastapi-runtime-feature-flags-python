"""配置拒绝原因：按来源与原因可区分。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RejectReason(str, Enum):
    TYPE_MISMATCH = "type_mismatch"      # 类型不合法
    MISSING_REQUIRED = "missing_required"  # 必填缺失
    UNKNOWN_KEY = "unknown_key"          # 未知项
    INVALID_VALUE = "invalid_value"      # 值域不合法（如百分比越界）
    DEPENDENCY_CYCLE = "dependency_cycle"    # 开关依赖关系成环
    RELATION_CONFLICT = "relation_conflict"  # 互斥组内冲突（并列优先级/组内依赖）


@dataclass(frozen=True)
class ConfigRejection:
    """一条配置拒绝记录：来源 + 键 + 原因 + 说明。"""
    source: str
    key: str
    reason: RejectReason
    detail: str = ""


@dataclass
class RejectionReport:
    """一次加载/刷新中收集到的全部拒绝记录。"""
    rejections: list[ConfigRejection] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejections

    def add(self, source: str, key: str, reason: RejectReason, detail: str = "") -> None:
        self.rejections.append(ConfigRejection(source=source, key=key, reason=reason, detail=detail))
