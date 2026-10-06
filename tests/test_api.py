"""HTTP 层冒烟：刷新/求值/预检/覆盖/审计全链路。"""
import logging

from fastapi.testclient import TestClient

from app.api import create_app
from app.flags.model import FlagSet
from app.flags.overrides import OverrideManager
from app.runtime.store import ConfigStore, Snapshot

log = logging.getLogger(__name__)


def make_client(now):
    store = ConfigStore(
        Snapshot(1, {"service_name": "ff", "api_token": "tok"}, FlagSet(1)),
        overrides=OverrideManager(lambda: now[0]),
    )
    return TestClient(create_app(store, clock=lambda: now[0])), store


def test_full_flow(caplog):
    caplog.set_level(logging.INFO)
    now = [1000.0]
    client, store = make_client(now)

    # 预检：合法候选
    body = {"flags": {
        "base": {"type": "bool", "default": True},
        "feat": {"type": "bool", "default": False, "depends_on": ["base"]},
    }}
    pre = client.post("/preflight", json=body).json()
    log.info("预检: ok=%s 摘要=%s", pre["ok"], pre["summary"])
    assert pre["ok"] and pre["summary"]["flags_added"] == ["base", "feat"]

    # 预检：成环候选被拒绝
    bad = client.post("/preflight", json={"flags": {
        "x": {"depends_on": ["y"]}, "y": {"depends_on": ["x"]}}}).json()
    assert not bad["ok"]
    assert bad["report"]["rejections"][0]["reason"] == "dependency_cycle"

    # 刷新生效
    r = client.post("/refresh", json=body).json()
    assert r["ok"] and r["version"] == 2

    # 求值：feat 依赖 base（base 生效）-> 取默认值 False
    d = client.post("/evaluate/feat", json={}).json()
    log.info("求值: %s", d)
    assert d["kind"] == "default" and d["value"] is False

    # 临时覆盖强制打开，到期后恢复
    client.post("/overrides", json={"flag": "feat", "value": True, "ttl_seconds": 30})
    assert client.post("/evaluate/feat", json={}).json()["kind"] == "override"
    now[0] = 1031.0
    assert client.post("/evaluate/feat", json={}).json()["kind"] == "default"
    events = client.get("/overrides/events").json()
    assert [e["action"] for e in events] == ["set", "expired"]

    # 配置脱敏
    assert client.get("/config").json()["config"]["api_token"] == "***"

    # 回滚
    rb = client.post("/rollback/1").json()
    assert rb["ok"] and store.current().version == 1
    log.info("审计: %s", [a["action"] for a in client.get("/audit").json()])
