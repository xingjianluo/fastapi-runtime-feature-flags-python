"""测试共享夹具与输入/决策日志。

每个测试在标题与 INFO 日志中打印：
- 输入配置（敏感字段以 ******** 形式出现）
- 最终决策依据（basis / 来源 / 版本）
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.models import FieldSpec
from app.service import ConfigService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("tests")

SETTING_SPECS = {
    "db.host": FieldSpec("db.host", "str", required=True),
    "db.port": FieldSpec("db.port", "int", default=5432, min=1, max=65535),
    "db.pool_size": FieldSpec(
        "db.pool_size", "int", default=10, min=1, max=100
    ),
    "db.password": FieldSpec("db.password", "str", secret=True),
    "log_level": FieldSpec(
        "log_level", "str", default="info",
        enum=("debug", "info", "warn", "error"),
    ),
}

DEFAULTS = {
    "db.host": "localhost",
    "db.port": 5432,
    "db.pool_size": 10,
    "log_level": "info",
}

BASE_FLAGS = {
    "new_checkout": {
        "type": "bool",
        "default": False,
        "percentage": 0,
        "rules": [
            {
                "name": "staff_rollout",
                "effect": "on",
                "conditions": [
                    {"attribute": "email", "op": "prefix", "value": "staff@"}
                ],
            },
            {
                "name": "block_free",
                "effect": "off",
                "conditions": [
                    {"attribute": "tier", "op": "eq", "value": "free"}
                ],
            },
        ],
    },
    "search_limit": {"type": "int", "default": 10, "percentage": 0},
}


def write_config(tmp_path: Path, body: dict) -> str:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


def base_config(tmp_path: Path, **overrides) -> str:
    body = {
        "db": {"host": "db.internal", "port": 5432, "pool_size": 20},
        "log_level": "info",
        "feature_flags": BASE_FLAGS,
    }
    body.update(overrides)
    return write_config(tmp_path, body)


def make_service(path: str | None) -> ConfigService:
    return ConfigService(SETTING_SPECS, DEFAULTS, config_path=path)


@pytest.fixture
def service(tmp_path: Path) -> ConfigService:
    path = base_config(tmp_path)
    svc = make_service(path)
    svc.load_initial()
    log.info("fixture service loaded from %s", path)
    return svc
