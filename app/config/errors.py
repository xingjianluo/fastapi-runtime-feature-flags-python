"""配置拒绝原因：按来源与原因可区分。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RejectReason(str, Enum):
    TYPE_MISMATCH = "type_mismatch"      # 类型不合法
    MISSING_REQUIRED = "missing_required"  # 必填缺失
    UNKNOWN_KEY = "unknown_key"          # 未知项
    INVALID_VALUE = "invalid_value"      # 值域不合法（如百分比越界）


#: 致命原因：出现即整体拒绝；UNKNOWN_KEY 仅丢弃该键，不阻断合并
FATAL_REASONS: frozenset[RejectReason] = frozenset({
    RejectReason.TYPE_MISMATCH,
    RejectReason.MISSING_REQUIRED,
    RejectReason.INVALID_VALUE,
})


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

    @property
    def has_fatal(self) -> bool:
        """是否存在致命拒绝（出现即应整体拒绝本次变更）。"""
        return any(r.reason in FATAL_REASONS for r in self.rejections)

    def add(self, source: str, key: str, reason: RejectReason, detail: str = "") -> None:
        self.rejections.append(ConfigRejection(source=source, key=key, reason=reason, detail=detail))
