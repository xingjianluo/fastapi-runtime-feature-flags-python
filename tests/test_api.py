import logging

from fastapi.testclient import TestClient

from app.api import create_app
from app.config.schema import SCHEMA, ValueType
from app.flags.model import Flag, FlagSet
from app.runtime.overrides import OverrideManager
from app.runtime.store import ConfigStore, Snapshot

log = logging.getLogger(__name__)


def make_client():
    config = {f.name: f.default for f in SCHEMA}
    config["api_token"] = "secret-token"
    flags = FlagSet(version=1, flags={
        "checkout": Flag("checkout", ValueType.BOOL, default=False),
    })
    store = ConfigStore(Snapshot(version=1, config=config, flags=flags))
    clock = [1000.0]
    om = OverrideManager(clock=lambda: clock[0])
    return TestClient(create_app(store, om)), store, clock


def test_config_masked():
    client, _, _ = make_client()
    body = client.get("/config").json()
    log.info("GET /config -> %s", body)
    assert body["config"]["api_token"] == "***"
    assert body["config"]["service_name"] == "feature-flags"


def test_evaluate_and_refresh_cycle():
    client, store, _ = make_client()
    r = client.post("/evaluate", json={"flag": "checkout", "context": {}}).json()
    log.info("刷新前求值=%s", r)
    assert r["kind"] == "default" and r["value"] is False

    payload = {"actor": "ops", "flags": {
        "checkout": {"type": "bool", "default": True},
    }}
    pre = client.post("/preflight", json=payload).json()
    log.info("预检=%s", pre)
    assert pre["ok"] and any(d["name"] == "checkout" for d in pre["flag_diffs"])

    r = client.post("/refresh", json=payload).json()
    assert r["ok"] and r["version"] == 2
    r = client.post("/evaluate", json={"flag": "checkout", "context": {}}).json()
    log.info("刷新后求值=%s", r)
    assert r["value"] is True

    r = client.post("/rollback", json={"version": 1, "actor": "ops"}).json()
    assert r["ok"]
    r = client.post("/evaluate", json={"flag": "checkout", "context": {}}).json()
    assert r["value"] is False


def test_refresh_with_cycle_rejected_via_api():
    client, store, _ = make_client()
    payload = {"flags": {
        "a": {"type": "bool", "default": True, "depends_on": ["b"]},
        "b": {"type": "bool", "default": True, "depends_on": ["a"]},
    }}
    r = client.post("/refresh", json=payload).json()
    log.info("成环刷新响应=%s", r)
    assert r["ok"] is False
    assert any("依赖环" in rej["detail"] for rej in r["preflight"]["rejections"])
    assert store.current().version == 1  # 线上状态不变


def test_override_via_api_and_masking():
    client, store, clock = make_client()
    r = client.post("/overrides", json={
        "flag": "checkout", "value": True, "ttl_seconds": 60,
        "actor": "ops", "reason": "演练",
    }).json()
    assert r["expires_at"] == 1060.0
    d = client.post("/evaluate", json={"flag": "checkout", "context": {}}).json()
    log.info("覆盖期间求值=%s", d)
    assert d["kind"] == "override" and d["value"] is True

    clock[0] += 61  # 到期
    d = client.post("/evaluate", json={"flag": "checkout", "context": {}}).json()
    assert d["kind"] == "default" and d["value"] is False

    body = client.get("/overrides").json()
    log.info("覆盖事件=%s", body["events"])
    assert [e["action"] for e in body["events"]] == ["set", "expired"]
