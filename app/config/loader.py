"""多来源配置加载与合并：defaults < file < env，优先级确定。

任何类型不合法、必填缺失或未知项都会记录为 ConfigRejection，
合并结果整体拒绝（返回 None），绝不静默使用兜底值。
"""
from __future__ import annotations

import json
import os
from typing import Any

from app.config.errors import RejectionReport, RejectReason
from app.config.schema import SCHEMA, ConfigField, ValueType

MASK = "***"

_TRUE_WORDS = {"true", "1", "yes", "on"}
_FALSE_WORDS = {"false", "0", "no", "off"}


def check_type(value: Any, expected: ValueType) -> bool:
    """严格类型检查（bool 不算 int）。"""
    if expected is ValueType.BOOL:
        return isinstance(value, bool)
    if expected is ValueType.INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is ValueType.FLOAT:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected is ValueType.STR:
        return isinstance(value, str)
    return False


def parse_env_value(raw: str, expected: ValueType) -> tuple[bool, Any]:
    """把环境变量字符串解析为目标类型；失败返回 (False, None)。"""
    try:
        if expected is ValueType.BOOL:
            low = raw.strip().lower()
            if low in _TRUE_WORDS:
                return True, True
            if low in _FALSE_WORDS:
                return True, False
            return False, None
        if expected is ValueType.INT:
            return True, int(raw.strip())
        if expected is ValueType.FLOAT:
            return True, float(raw.strip())
        return True, raw
    except ValueError:
        return False, None


def _validate_entries(
    values: dict[str, Any],
    source: str,
    report: RejectionReport,
    schema: tuple[ConfigField, ...],
) -> dict[str, Any]:
    """校验一个来源的键值：未知项与类型错误记入 report，返回合法子集。"""
    valid: dict[str, Any] = {}
    for key, value in values.items():
        field = next((f for f in schema if f.name == key), None)
        if field is None:
            report.add(source, key, RejectReason.UNKNOWN_KEY, "未在 schema 中声明")
            continue
        if not check_type(value, field.type):
            report.add(
                source, key, RejectReason.TYPE_MISMATCH,
                f"期望 {field.type.value}，实际 {type(value).__name__}",
            )
            continue
        valid[key] = value
    return valid


def load_file_source(
    path: str,
    report: RejectionReport,
    schema: tuple[ConfigField, ...] = SCHEMA,
) -> dict[str, Any]:
    """从 JSON 配置文件加载并校验；文件本身非法时整体拒绝该来源。"""
    source = f"file:{path}"
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        report.add(source, "<file>", RejectReason.INVALID_VALUE, f"文件不可读或不是合法 JSON: {exc}")
        return {}
    if not isinstance(data, dict):
        report.add(source, "<file>", RejectReason.TYPE_MISMATCH, "配置文件顶层必须是 JSON 对象")
        return {}
    return _validate_entries(data, source, report, schema)


def load_env_source(
    environ: dict[str, str] | None = None,
    prefix: str = "APP_",
    report: RejectionReport | None = None,
    schema: tuple[ConfigField, ...] = SCHEMA,
) -> dict[str, Any]:
    """从环境变量加载：APP_SERVICE_NAME -> service_name，按声明类型解析。"""
    if environ is None:
        environ = dict(os.environ)
    if report is None:
        report = RejectionReport()
    source = "env"
    parsed: dict[str, Any] = {}
    for env_key, raw_value in environ.items():
        if not env_key.startswith(prefix):
            continue
        name = env_key[len(prefix):].lower()
        field = next((f for f in schema if f.name == name), None)
        if field is None:
            report.add(source, name, RejectReason.UNKNOWN_KEY,
                       f"环境变量 {prefix}{name.upper()} 未在 schema 中声明")
            continue
        ok, value = parse_env_value(raw_value, field.type)
        if not ok:
            report.add(source, name, RejectReason.TYPE_MISMATCH,
                       f"环境变量值 {raw_value!r} 无法解析为 {field.type.value}")
            continue
        parsed[name] = value
    return parsed


def merge_config(
    file_values: dict[str, Any] | None = None,
    env_values: dict[str, Any] | None = None,
    schema: tuple[ConfigField, ...] = SCHEMA,
    prior_report: RejectionReport | None = None,
) -> tuple[dict[str, Any] | None, RejectionReport]:
    """合并 defaults < file < env。

    返回 (config, report)：report 非空时 config 为 None（整体拒绝）。
    prior_report 可传入加载期已收集的拒绝记录，一并计入结论。
    """
    report = RejectionReport()
    if prior_report is not None:
        report.rejections.extend(prior_report.rejections)
    merged: dict[str, Any] = {f.name: f.default for f in schema}
    for source, values in (("file", file_values or {}), ("env", env_values or {})):
        merged.update(_validate_entries(values, source, report, schema))
    for f in schema:
        if f.required and merged.get(f.name) is None:
            report.add("merged", f.name, RejectReason.MISSING_REQUIRED, "必填项在所有来源中均缺失")
    if not report.ok:
        return None, report
    return merged, report


def load_and_merge(
    file_path: str | None = None,
    environ: dict[str, str] | None = None,
    prefix: str = "APP_",
    schema: tuple[ConfigField, ...] = SCHEMA,
) -> tuple[dict[str, Any] | None, RejectionReport]:
    """完整管线：加载 file/env 来源并合并，一次给出最终结论。"""
    report = RejectionReport()
    file_values = load_file_source(file_path, report, schema) if file_path else {}
    env_values = load_env_source(environ, prefix, report, schema)
    return merge_config(file_values, env_values, schema, prior_report=report)


def mask_config(config: dict[str, Any], schema: tuple[ConfigField, ...] = SCHEMA) -> dict[str, Any]:
    """对外展示用：敏感字段脱敏。"""
    return {
        f.name: (MASK if f.sensitive and config.get(f.name) else config.get(f.name))
        for f in schema
    }
