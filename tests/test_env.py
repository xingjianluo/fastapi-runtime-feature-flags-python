"""服务层环境变量入口：优先级在加载时生效且来源可解释。"""
from __future__ import annotations

import logging

from tests.conftest import base_config, make_service

log = logging.getLogger("tests")


def test_env_overrides_file_at_service_level(tmp_path, monkeypatch):
    path = base_config(tmp_path)
    monkeypatch.setenv("APP_DB__POOL_SIZE", "77")
    log.info("输入: file db.pool_size=20, env APP_DB__POOL_SIZE=77")
    svc = make_service(path)
    svc.load_initial()
    setting = svc.view()["settings"]["db.pool_size"]
    assert setting["value"] == 77
    assert setting["source"] == "env:APP_DB__POOL_SIZE"
    log.info("最终决策来源: %s = %s", setting["source"], setting["value"])


def test_unknown_env_rejected_at_load(tmp_path, monkeypatch):
    path = base_config(tmp_path)
    monkeypatch.setenv("APP_NOT_DECLARED", "x")
    log.info("输入: 未注册环境变量 APP_NOT_DECLARED")
    svc = make_service(path)
    try:
        svc.load_initial()
    except Exception as exc:  # noqa: BLE001
        assert any(i.kind == "unknown" for i in exc.issues)
        log.info("加载被拒绝: %s", [(i.source, i.kind) for i in exc.issues])
    else:
        raise AssertionError("未注册环境变量应导致加载拒绝")
