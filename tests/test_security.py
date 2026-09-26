"""敏感信息保护：脱敏视图、审计与拒绝结论均不得泄露原值。"""
from __future__ import annotations

import logging

import pytest

from app.errors import ConfigError

log = logging.getLogger("tests")


def test_invalid_secret_value_is_redacted_in_reject(service, tmp_path):
    from app.models import FieldSpec
    from app.service import ConfigService
    from tests.conftest import DEFAULTS, base_config

    # 额外声明一个带枚举约束的敏感字段，使其非法值本会进入 reason
    specs = dict(service._specs)  # noqa: SLF001
    specs["db.token"] = FieldSpec(
        "db.token", "str", secret=True, enum=("allowed-a", "allowed-b")
    )
    svc = ConfigService(specs, DEFAULTS, config_path=base_config(tmp_path))
    svc.load_initial()

    secret = "SUPER-SECRET-DO-NOT-LEAK"
    log.info("输入: 敏感字段 db.token=%s（枚举外，应拒绝且脱敏）", secret)
    with pytest.raises(ConfigError) as exc:
        svc.refresh({"db": {"token": secret}})

    assert secret not in str(exc.value)
    for issue in exc.value.issues:
        assert secret not in str(issue)
        if issue.path == "db.token":
            assert "值已隐藏" in issue.reason
    for issue in exc.value.explain():
        assert secret not in str(issue)
        if issue["path"] == "db.token":
            assert "值已隐藏" in issue["reason"]
    for event in svc.audit_entries():
        assert secret not in str(event)
    log.info("敏感字段拒绝结论（已脱敏）: %s", exc.value.explain())


def test_redact_tree_helper():
    from app.service import _redact_tree
    out = _redact_tree(
        {"db": {"password": "x", "host": "h"}, "ok": 1},
        {"db.password"},
    )
    assert out["db"]["password"] == "********"
    assert out["db"]["host"] == "h"
    assert out["ok"] == 1
    log.info("递归脱敏结论: %s", out)
