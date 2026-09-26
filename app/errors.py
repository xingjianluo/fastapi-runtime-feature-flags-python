"""错误模型：配置拒绝结论必须带来源与原因，可区分、可解释。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Issue:
    """单条配置问题。

    source: 出问题的来源（defaults / file:<path> / env）
    path:   配置项路径，如 db.password
    kind:   missing（必填缺失）/ type（类型不合法）/
            unknown（出现未知项）/ constraint（范围/枚举不合法）
    reason: 人类可读原因
    value:  原始值（敏感值仅在对外展示层脱敏，问题对象保留原值便于内部排查）
    """

    source: str
    path: str
    kind: str
    reason: str
    value: Any = None

    def describe(self) -> str:
        return f"[{self.kind}] {self.path} @ {self.source}: {self.reason}"


class ConfigError(Exception):
    """配置校验失败：携带全部问题，绝不静默使用兜底值。"""

    def __init__(self, issues: list[Issue]) -> None:
        if not issues:
            raise ValueError("ConfigError requires at least one Issue")
        self.issues: list[Issue] = list(issues)
        super().__init__("; ".join(i.describe() for i in self.issues))

    def by_kind(self) -> dict[str, list[Issue]]:
        grouped: dict[str, list[Issue]] = {}
        for issue in self.issues:
            grouped.setdefault(issue.kind, []).append(issue)
        return grouped

    def explain(self) -> list[dict[str, Any]]:
        """结构化的拒绝结论（敏感值不在此输出，由展示层决定）。"""
        import json  # noqa: F401  (保持依赖最小，直接字面量构造)

        return [
            {
                "source": i.source,
                "path": i.path,
                "kind": i.kind,
                "reason": i.reason,
            }
            for i in self.issues
        ]
