"""非法配置拒绝与来源优先级：失败原因必须按来源/kind 可区分。"""
from __future__ import annotations

import logging

import pytest

from app.errors import ConfigError
from app.merge import load_env_values, merge_and_validate
from app.models import FieldSpec
from tests.conftest import DEFAULTS, SETTING_SPECS

log = logging.getLogger("tests")


def _merge(file_values=None, env=None, names=None, override=None, specs=None):
    specs = specs or SETTING_SPECS
    return merge_and_validate(
        defaults=DEFAULTS,
        file_values=file_values,
        file_source="file:config.json",
        env_values=env,
        env_names=names or {},
        specs=specs,
        override_values=override,
    )


def test_priority_override_env_file_defaults(monkeypatch):
    monkeypatch.setenv("APP_DB__POOL_SIZE", "30")
    env_values, env_names = load_env_values("APP_")
    log.info("输入: file={db.pool_size:20}, env=%s, override={db.pool_size:40}",
             env_values.get("db.pool_size"))
    cfg = _merge(
        file_values={"db.pool_size": 20, "db.host": "h"},
        env=env_values, names=env_names,
        override={"db.pool_size": 40},
    )
    assert cfg.value_of("db.pool_size") == 40
    assert cfg.settings["db.pool_size"].source == "api:refresh"
    log.info("结论来源: %s", cfg.settings["db.pool_size"].source)

    # env 高于 file
    cfg2 = _merge(
        file_values={"db.pool_size": 20, "db.host": "h"},
        env=env_values, names=env_names,
    )
    assert cfg2.value_of("db.pool_size") == 30
    assert cfg2.settings["db.pool_size"].source.startswith("env:")

    # file 高于 defaults
    cfg3 = _merge(file_values={"db.pool_size": 20, "db.host": "h"})
    assert cfg3.value_of("db.pool_size") == 20

    # 无高优先级来源时使用 defaults
    cfg4 = _merge(file_values={"db.host": "h"})
    assert cfg4.value_of("db.pool_size") == 10
    assert cfg4.settings["db.pool_size"].source == "defaults"


def test_required_missing_is_rejected():
    log.info("输入: 所有来源均缺少必填项 db.host（defaults 也不提供）")
    with pytest.raises(ConfigError) as exc:
        merge_and_validate(
            defaults={"db.port": 5432},
            file_values={"db.port": 5432},
            file_source="file:config.json",
            env_values=None, env_names=None,
            specs=SETTING_SPECS,
        )
    kinds = {(i.kind, i.path) for i in exc.value.issues}
    assert ("missing", "db.host") in kinds
    log.info("拒绝结论: %s", exc.value.explain())


def test_invalid_type_does_not_fall_back():
    # file 提供了非法 int：不得静默回退到 defaults 的 5432
    log.info("输入: file db.port='oops'（非法）, defaults db.port=5432")
    with pytest.raises(ConfigError) as exc:
        _merge(file_values={"db.host": "h", "db.port": "oops"})
    issue = next(i for i in exc.value.issues if i.path == "db.port")
    assert issue.kind == "type"
    assert issue.source == "file:config.json"
    log.info("拒绝结论（未回退默认值）: %s", issue.describe())


def test_unknown_keys_rejected_by_source():
    log.info("输入: file 出现 unknown.file.key, env 出现 UNKNOWN_ENV")
    with pytest.raises(ConfigError) as exc:
        _merge(
            file_values={"db.host": "h", "unknown.file.key": 1},
            env={"unknown.env": "1"},
            names={"unknown.env": "APP_UNKNOWN_ENV"},
        )
    by_path = {i.path: i for i in exc.value.issues}
    assert by_path["unknown.file.key"].kind == "unknown"
    assert by_path["unknown.file.key"].source == "file:config.json"
    assert by_path["unknown.env"].source == "env:APP_UNKNOWN_ENV"
    log.info("拒绝结论: %s", exc.value.explain())


def test_constraint_enum_and_range():
    log.info("输入: log_level=trace（枚举外）, db.pool_size=999（超上限）")
    with pytest.raises(ConfigError) as exc:
        _merge(file_values={
            "db.host": "h", "log_level": "trace", "db.pool_size": 999,
        })
    kinds = {i.path: i.kind for i in exc.value.issues}
    assert kinds == {"log_level": "constraint", "db.pool_size": "constraint"}
    log.info("拒绝结论: %s", exc.value.explain())


def test_bool_and_json_coercion_from_env(monkeypatch):
    specs = {
        "feature.enabled": FieldSpec("feature.enabled", "bool"),
        "feature.meta": FieldSpec("feature.meta", "json"),
    }
    monkeypatch.setenv("APP_FEATURE__ENABLED", "yes")
    monkeypatch.setenv("APP_FEATURE__META", '{"a": 1}')
    env_values, env_names = load_env_values("APP_")
    cfg = merge_and_validate(
        defaults={}, file_values=None, file_source="file:none",
        env_values=env_values, env_names=env_names, specs=specs,
    )
    assert cfg.value_of("feature.enabled") is True
    assert cfg.value_of("feature.meta") == {"a": 1}
    log.info("环境变量类型转换结论: enabled=%s meta=%s",
             cfg.value_of("feature.enabled"), cfg.value_of("feature.meta"))


def test_invalid_bool_from_env(monkeypatch):
    specs = {"feature.enabled": FieldSpec("feature.enabled", "bool")}
    monkeypatch.setenv("APP_FEATURE__ENABLED", "maybe")
    env_values, env_names = load_env_values("APP_")
    log.info("输入: env APP_FEATURE__ENABLED=maybe")
    with pytest.raises(ConfigError) as exc:
        merge_and_validate(
            defaults={}, file_values=None, file_source="file:none",
            env_values=env_values, env_names=env_names, specs=specs,
        )
    assert exc.value.issues[0].kind == "type"
    assert exc.value.issues[0].source == "env:APP_FEATURE__ENABLED"
