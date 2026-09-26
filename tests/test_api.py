"""脱敏与 HTTP 入口一致性测试。"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.errors import ConfigError

log = logging.getLogger("tests")


@pytest.fixture
def client(service):
    return TestClient(create_app(service))


def test_config_view_masks_secret(client):
    log.info("输入: 通过刷新注入 db.password=s3cr3t（敏感）")
    r = client.post("/refresh", json={"config": {"db": {"password": "s3cr3t"}}})
    assert r.status_code == 200
    resp = client.get("/config")
    assert resp.status_code == 200
    pw = resp.json()["settings"]["db.password"]["value"]
    assert pw == "********"
    log.info("对外结论 db.password=%r", pw)
    # 显式带 include_secrets 用于内网排障
    resp2 = client.get("/config?include_secrets=true")
    assert resp2.json()["settings"]["db.password"]["value"] == "s3cr3t"


def test_flag_decision_endpoint(client):
    resp = client.get(
        "/flags/new_checkout",
        params={"identity": "u1", "email": "staff@x.com", "tier": "free"},
    )
    body = resp.json()
    assert body["enabled"] is True
    assert body["basis"] == "rule:staff_rollout"
    log.info("HTTP 决策输入 email=staff@x.com&tier=free => basis=%s", body["basis"])


def test_flag_404(client):
    assert client.get("/flags/not_there", params={"identity": "u"}).status_code == 404


def test_refresh_endpoint_success_and_reject(client):
    ok = client.post("/refresh", json={
        "config": {"feature_flags": {"new_checkout": {
            "type": "bool", "default": False, "percentage": 100,
        }}}
    })
    assert ok.status_code == 200 and ok.json()["status"] == "applied"

    log.info("输入非法刷新: db.port='bad', unknown=1")
    bad = client.post("/refresh", json={
        "config": {"db": {"port": "bad"}, "unknown": 1}
    })
    assert bad.status_code == 422
    detail = bad.json()["detail"]
    assert detail["error"] == "config_rejected"
    kinds = {i["kind"] for i in detail["issues"]}
    assert kinds == {"type", "unknown"}
    log.info("HTTP 拒绝结论 kinds=%s，当前版本保持 v%s",
             kinds, detail["current_version"])

    # 线上仍是刚成功的版本
    assert client.get("/health").json()["version"] == detail["current_version"]


def test_rollback_endpoint(client):
    client.post("/refresh", json={"config": {"db": {"host": "h2"}}})
    r = client.post("/rollback", json={"version": 1, "reason": "验证回滚"})
    assert r.status_code == 200 and r.json()["status"] == "rolled_back"
    assert client.get("/config").json()["settings"]["db.host"]["value"] == "db.internal"
    events = client.get("/audit").json()["events"]
    assert [e["action"] for e in events] == ["load", "refresh", "rollback"]
    log.info("HTTP 回滚后审计: %s", [e["action"] for e in events])


def test_secret_value_not_leaked_in_reject_audit(service, client):
    # 敏感字段类型非法：审计与响应都不得出现原值
    log.info("输入: db.password=12345（int 赋给 str 以外路径不会触发；改用未知项覆盖）")
    # 直接验证 service 层 sanitize：给敏感项制造 enum/type 错误不可行(str)，
    # 因此验证 refresh 输入在日志/审计中被脱敏。
    with pytest.raises(ConfigError):
        service.refresh({"db": {"password": "topsecret-value"}, "bad": 1})
    for event in service.audit_entries():
        assert "topsecret-value" not in str(event)
    log.info("审计记录中未出现敏感原值")
