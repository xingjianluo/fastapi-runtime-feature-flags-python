"""HTTP 入口：配置查看（脱敏）、开关求值、刷新与回滚。"""
from __future__ import annotations

from fastapi import FastAPI

from app.runtime.store import ConfigStore


def create_app(store: ConfigStore) -> FastAPI:
    app = FastAPI(title="runtime-feature-flags")
    # 路由在后续步骤填充
    app.state.store = store
    return app
