"""多来源配置加载、合并与校验。

优先级（高 -> 低）：env > file > defaults。
任一来源出现 类型不合法 / 必填缺失 / 未知项 / 约束违反，
都收集为 Issue 并整体拒绝，绝不静默回退到低优先级兜底值。
"""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

from app.errors import ConfigError, Issue
from app.models import FieldSpec, MergedConfig, Setting

_BOOL_TRUE = {"true", "1", "yes", "on"}
_BOOL_FALSE = {"false", "0", "no", "off"}


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            flat.update(_flatten(value, path))
        else:
            flat[path] = value
    return flat


def load_file_raw(path: str) -> tuple[dict[str, Any], str]:
    """读取配置文件的原始（未扁平化）内容。"""
    flat, source = load_file_values(path)
    suffix = Path(path).suffix.lower()
    raw = Path(path).read_text(encoding="utf-8")
    if suffix == ".json":
        data = json.loads(raw)
    else:
        data = tomllib.loads(raw)
    if not isinstance(data, dict):
        raise ConfigError(
            [Issue(source, "", "type", "顶层必须是对象/表")]
        )
    return data, source


def load_file_values(path: str) -> tuple[dict[str, Any], str]:
    """读取 JSON/TOML 文件并扁平化，返回 (值字典, 来源标识)。

    文件不可读或解析失败时抛出 ConfigError，而非静默忽略。
    """
    source = f"file:{path}"
    p = Path(path)
    if not p.exists():
        raise ConfigError(
            [Issue(source, "", "missing", f"配置文件不存在: {path}")]
        )
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 取决于文件系统
        raise ConfigError(
            [Issue(source, "", "constraint", f"配置文件不可读: {exc}")]
        ) from exc
    suffix = p.suffix.lower()
    try:
        if suffix == ".json":
            data = json.loads(raw)
        elif suffix == ".toml":
            data = tomllib.loads(raw)
        else:
            raise ConfigError(
                [Issue(source, "", "constraint", f"不支持的文件类型: {suffix}")]
            )
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(
            [Issue(source, "", "constraint", f"配置文件解析失败: {exc}")]
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(
            [Issue(source, "", "type", "顶层必须是对象/表")]
        )
    return _flatten(data), source


def load_env_values(prefix: str = "APP_") -> tuple[dict[str, str], dict[str, str]]:
    """读取带前缀的环境变量，双下划线分隔层级，如 APP_DB__PASSWORD。

    返回 (扁平值字典, 键 -> 原始变量名)，变量名用于可区分的来源结论。
    """
    values: dict[str, str] = {}
    names: dict[str, str] = {}
    for name, raw in os.environ.items():
        if name.startswith(prefix):
            key = name[len(prefix):].lower().replace("__", ".")
            values[key] = raw
            names[key] = name
    return values, names


def _coerce(spec: FieldSpec, raw: Any, source: str) -> tuple[Any, Issue | None]:
    """按声明类型转换；失败返回 Issue，而不是吞掉异常用默认值。"""
    t = spec.type
    try:
        if t == "bool":
            if isinstance(raw, bool):
                return raw, None
            if isinstance(raw, str):
                low = raw.strip().lower()
                if low in _BOOL_TRUE:
                    return True, None
                if low in _BOOL_FALSE:
                    return False, None
                raise ValueError(f"无法解析为布尔值: {raw!r}（允许 true/false/1/0）")
            raise ValueError(f"期望 bool，实际 {type(raw).__name__}")
        if t == "int":
            if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                raise ValueError(f"期望 int，实际 {type(raw).__name__}")
            if isinstance(raw, str):
                return int(raw.strip()), None
            return int(raw), None
        if t == "float":
            if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
                raise ValueError(f"期望 float，实际 {type(raw).__name__}")
            return float(raw), None
        if t == "str":
            if not isinstance(raw, str):
                raise ValueError(f"期望 str，实际 {type(raw).__name__}")
            return raw, None
        if t == "json":
            if isinstance(raw, str):
                return json.loads(raw), None
            return raw, None
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, Issue(
            source, spec.key, "type", f"类型 {t} 转换失败: {exc}", raw
        )
    return None, Issue(source, spec.key, "type", f"未知类型 {t}")  # pragma: no cover


def _check_constraints(spec: FieldSpec, value: Any, source: str) -> Issue | None:
    if spec.enum is not None and value not in spec.enum:
        return Issue(
            source, spec.key, "constraint",
            f"值 {value!r} 不在允许集合 {list(spec.enum)!r} 中", value,
        )
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if spec.min is not None and value < spec.min:
            return Issue(
                source, spec.key, "constraint",
                f"值 {value} 小于最小值 {spec.min}", value,
            )
        if spec.max is not None and value > spec.max:
            return Issue(
                source, spec.key, "constraint",
                f"值 {value} 大于最大值 {spec.max}", value,
            )
    return None


# 来源优先级（高 -> 低）：override > env > file > defaults
def merge_and_validate(
    defaults: dict[str, Any],
    file_values: dict[str, Any] | None,
    file_source: str,
    env_values: dict[str, str] | None,
    env_names: dict[str, str] | None,
    specs: dict[str, FieldSpec],
    deny_unknown: bool = True,
    override_values: dict[str, Any] | None = None,
    override_source: str = "api:refresh",
) -> MergedConfig:
    """合并多来源并严格校验，全部通过才返回不可变快照。"""
    file_values = file_values or {}
    env_values = env_values or {}
    env_names = env_names or {}
    override_values = override_values or {}
    issues: list[Issue] = []

    def env_src(key: str) -> str:
        name = env_names.get(key)
        return f"env:{name}" if name else "env"

    if deny_unknown:
        known = set(specs)
        for key in override_values:
            if key not in known:
                issues.append(Issue(
                    override_source, key, "unknown",
                    "刷新输入中存在未注册配置项", override_values[key],
                ))
        for key in env_values:
            if key not in known:
                issues.append(Issue(
                    env_src(key), key, "unknown", "环境变量未在配置声明中注册",
                    env_values[key],
                ))
        for key in file_values:
            if key not in known:
                issues.append(Issue(
                    file_source, key, "unknown", "文件配置项未在配置声明中注册",
                    file_values[key],
                ))
        for key in defaults:
            if key not in known:
                issues.append(Issue(
                    "defaults", key, "unknown", "默认配置项未在配置声明中注册",
                    defaults[key],
                ))

    settings: dict[str, Setting] = {}
    for key, spec in specs.items():
        if spec.key != key:
            issues.append(Issue(
                "defaults", key, "constraint",
                f"FieldSpec 声明键不一致: {spec.key!r} != {key!r}",
            ))
            continue

        # 按优先级查找：高优先级来源若提供了非法值，直接拒绝，不向下回退。
        chosen: tuple[Any, str] | None = None
        if key in override_values:
            chosen = (override_values[key], override_source)
        elif key in env_values:
            chosen = (env_values[key], env_src(key))
        elif key in file_values:
            chosen = (file_values[key], file_source)
        elif key in defaults:
            chosen = (defaults[key], "defaults")

        if chosen is None:
            if spec.required:
                issues.append(Issue(
                    "all", key, "missing", "必填项在任何来源中均未提供",
                ))
            continue

        raw, source = chosen
        value, issue = _coerce(spec, raw, source)
        if issue is not None:
            issues.append(issue)
            continue
        issue = _check_constraints(spec, value, source)
        if issue is not None:
            issues.append(issue)
            continue
        settings[key] = Setting(key=key, value=value, source=source)

    if issues:
        raise ConfigError(issues)

    sources = tuple(
        (["defaults"] if defaults else [])
        + ([file_source] if file_values else [])
        + ([env_src(k) for k in env_values])
        + ([override_source] if override_values else [])
    )
    return MergedConfig(settings=settings, sources=sources)
