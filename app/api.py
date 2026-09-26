"""FastAPI 入口：HTTP 与程序化调用共享同一个 ConfigService，
因此配置来源、灰度判定、刷新/回滚的结论在各入口下完全一致。"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.errors import ConfigError
from app.service import ConfigService


class DecisionRequest(BaseModel):
    identity: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class RefreshRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


class RollbackRequest(BaseModel):
    version: int
    reason: str | None = None


def create_app(svc: ConfigService) -> FastAPI:
    app = FastAPI(
        title="runtime-config-feature-flags",
        version="0.1.0",
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": svc.current_version()}

    @app.get("/config")
    def get_config(include_secrets: bool = False) -> dict[str, Any]:
        # include_secrets 仅用于本地/内网排障；默认视图全部脱敏。
        return svc.view(include_secrets=include_secrets)

    @app.get("/flags/{key}")
    def evaluate_flag(
        key: str,
        identity: str | None = None,
        email: str | None = None,
        tier: str | None = None,
    ) -> dict[str, Any]:
        attributes: dict[str, Any] = {}
        if email is not None:
            attributes["email"] = email
        if tier is not None:
            attributes["tier"] = tier
        try:
            d = svc.decision(key, identity=identity, attributes=attributes)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "flag": d.key,
            "enabled": d.enabled,
            "value": d.value,
            "basis": d.basis,
            "detail": d.detail,
            "version": d.version,
            "bucket": d.bucket,
            "trace": list(d.trace),
        }

    @app.post("/flags/{key}/decision")
    def decide_flag(key: str, req: DecisionRequest) -> dict[str, Any]:
        try:
            d = svc.decision(key, identity=req.identity, attributes=req.attributes)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "flag": d.key,
            "enabled": d.enabled,
            "value": d.value,
            "basis": d.basis,
            "detail": d.detail,
            "version": d.version,
            "bucket": d.bucket,
            "trace": list(d.trace),
        }

    @app.post("/refresh")
    def refresh(req: RefreshRequest) -> dict[str, Any]:
        try:
            version = svc.refresh(req.config)
        except ConfigError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "config_rejected",
                    "current_version": svc.current_version(),
                    "issues": exc.explain(),
                },
            ) from exc
        return {"status": "applied", "version": version}

    @app.post("/rollback")
    def rollback(req: RollbackRequest) -> dict[str, Any]:
        try:
            version = svc.rollback(req.version, reason=req.reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "rolled_back", "version": version}

    @app.get("/audit")
    def audit() -> dict[str, Any]:
        return {"events": svc.audit_entries()}

    return app
