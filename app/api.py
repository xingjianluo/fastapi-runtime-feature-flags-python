"""HTTP 入口：配置查看（脱敏）、开关求值、预检、刷新、回滚与临时覆盖。"""
from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any, Callable

from fastapi import FastAPI
from pydantic import BaseModel

from app.config.errors import RejectionReport
from app.config.loader import mask_config
from app.flags.overrides import Override, OverrideManager
from app.flags.serde import parse_flag_set
from app.runtime.preflight import preflight
from app.runtime.store import ConfigStore


class RefreshBody(BaseModel):
    config: dict[str, Any] | None = None
    flags: dict[str, Any] = {}
    actor: str = "system"


class OverrideBody(BaseModel):
    flag: str
    value: Any
    ttl_seconds: float
    actor: str = "system"
    reason: str = ""


def _report_dict(report: RejectionReport) -> dict[str, Any]:
    return {"ok": report.ok,
            "rejections": [asdict(r) for r in report.rejections]}


def create_app(store: ConfigStore, clock: Callable[[], float] = time.time) -> FastAPI:
    app = FastAPI(title="runtime-feature-flags")
    if store.overrides is None:
        store.overrides = OverrideManager(clock)
    app.state.store = store

    @app.get("/config")
    def get_config() -> dict[str, Any]:
        snap = store.current()
        return {"version": snap.version, "config": mask_config(snap.config)}

    @app.post("/evaluate/{flag_name}")
    def evaluate_flag(flag_name: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        decision = store.evaluate_flag(flag_name, context or {})
        return asdict(decision)

    @app.post("/preflight")
    def do_preflight(body: RefreshBody) -> dict[str, Any]:
        report = RejectionReport()
        snap = store.current()
        flags = parse_flag_set(body.flags, snap.flags.version + 1, report)
        result = preflight(snap, body.config, flags, prior_report=report)
        return {"ok": result.ok,
                "summary": asdict(result.summary),
                "report": _report_dict(result.report)}

    @app.post("/refresh")
    def do_refresh(body: RefreshBody) -> dict[str, Any]:
        report = RejectionReport()
        snap = store.current()
        flags = parse_flag_set(body.flags, snap.flags.version + 1, report)
        result = store.refresh(body.config, flags, report, actor=body.actor)
        return {"ok": result.ok, "version": result.version,
                "detail": result.detail, "report": _report_dict(result.report)}

    @app.post("/rollback/{version}")
    def do_rollback(version: int, actor: str = "system") -> dict[str, Any]:
        result = store.rollback(version, actor=actor)
        return {"ok": result.ok, "version": result.version, "detail": result.detail}

    @app.get("/audit")
    def get_audit() -> list[dict[str, Any]]:
        return [asdict(r) for r in store.audit_log()]

    @app.post("/overrides")
    def set_override(body: OverrideBody) -> dict[str, Any]:
        now = clock()
        override = Override(flag=body.flag, value=body.value,
                            expires_at=now + body.ttl_seconds, created_at=now,
                            actor=body.actor, reason=body.reason)
        store.overrides.set_override(override)
        return {"ok": True, "expires_at": override.expires_at}

    @app.delete("/overrides/{flag_name}")
    def revoke_override(flag_name: str, actor: str = "system") -> dict[str, Any]:
        return {"ok": store.overrides.revoke(flag_name, actor=actor)}

    @app.get("/overrides/events")
    def override_events() -> list[dict[str, Any]]:
        snap = store.current()
        sensitive = {n for n, f in snap.flags.flags.items() if f.sensitive}
        return [asdict(e) for e in store.overrides.masked_events(sensitive)]

    return app
