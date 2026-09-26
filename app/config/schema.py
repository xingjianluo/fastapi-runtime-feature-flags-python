"""配置项声明：类型、必填、敏感标记、默认值。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ValueType(str, Enum):
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STR = "str"


@dataclass(frozen=True)
class ConfigField:
    name: str
    type: ValueType
    required: bool = False
    sensitive: bool = False
    default: Any = None


# 服务配置 schema（骨架：先给少量字段，后续可扩展）
SCHEMA: tuple[ConfigField, ...] = (
    ConfigField("service_name", ValueType.STR, required=True, default="feature-flags"),
    ConfigField("max_connections", ValueType.INT, default=100),
    ConfigField("sample_rate", ValueType.FLOAT, default=1.0),
    ConfigField("debug", ValueType.BOOL, default=False),
    ConfigField("api_token", ValueType.STR, sensitive=True, default=""),
)

SCHEMA_BY_NAME: dict[str, ConfigField] = {f.name: f for f in SCHEMA}
