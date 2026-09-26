"""服务启动入口：声明配置规格，加载初始配置，装配 HTTP 入口。"""
from __future__ import annotations

import logging
import os

import uvicorn

from app.api import create_app
from app.models import FieldSpec
from app.service import ConfigService

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# 配置项声明：类型 / 必填 / 默认 / 敏感 / 约束
SETTING_SPECS: dict[str, FieldSpec] = {
    "db.host": FieldSpec("db.host", "str", required=True),
    "db.port": FieldSpec("db.port", "int", default=5432, min=1, max=65535),
    "db.pool_size": FieldSpec("db.pool_size", "int", default=10, min=1, max=100),
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

CONFIG_PATH = os.environ.get("APP_CONFIG_PATH", "examples/config.json")

service = ConfigService(
    SETTING_SPECS,
    DEFAULTS,
    config_path=CONFIG_PATH if os.path.exists(CONFIG_PATH) else None,
    env_prefix="APP_",
)
service.load_initial()

app = create_app(service)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
