"""配置体系的类型定义。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# 来源大类；具体来源标识形如 file:config.json
SourceName = Literal["defaults", "file", "env"]
ValueType = Literal["bool", "int", "float", "str", "json"]


@dataclass(frozen=True)
class FieldSpec:
    """普通配置项声明。"""

    key: str
    type: ValueType
    required: bool = False
    default: Any = None
    secret: bool = False
    min: float | None = None
    max: float | None = None
    enum: tuple[Any, ...] | None = None


@dataclass(frozen=True)
class Setting:
    """一条已生效配置及其确切来源。"""

    key: str
    value: Any
    source: str  # defaults / file:<path> / env:APP_DB__PASSWORD


@dataclass(frozen=True)
class MergedConfig:
    """一次合并的完整结果（不可变快照）。"""

    settings: dict[str, Setting] = field(default_factory=dict)
    sources: tuple[str, ...] = ()

    def value_of(self, key: str) -> Any:
        return self.settings[key].value

    def as_dict(self) -> dict[str, Any]:
        return {k: s.value for k, s in self.settings.items()}
