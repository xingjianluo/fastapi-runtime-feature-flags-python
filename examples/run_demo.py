"""本地手动验证：加载 -> 灰度判定 -> 失败刷新 -> 成功刷新 -> 回滚。"""
import json
import logging

from app.errors import ConfigError
from app.models import FieldSpec
from app.service import ConfigService

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

SETTING_SPECS = {
    "db.host": FieldSpec("db.host", "str", required=True),
    "db.port": FieldSpec("db.port", "int", default=5432, min=1, max=65535),
    "db.pool_size": FieldSpec("db.pool_size", "int", default=10, min=1, max=100),
    "db.password": FieldSpec("db.password", "str", required=False, secret=True),
    "log_level": FieldSpec(
        "log_level", "str", default="info", enum=("debug", "info", "warn", "error")
    ),
}

DEFAULTS = {"db.port": 5432, "db.pool_size": 10, "log_level": "info"}

svc = ConfigService(
    SETTING_SPECS, DEFAULTS,
    config_path="examples/config.json",
)
v1 = svc.load_initial({"db": {"password": "s3cr3t"}})
print("loaded v", v1)
print(json.dumps(svc.view(), ensure_ascii=False, indent=2))

d = svc.decision("new_checkout", identity="u-7", attributes={"email": "staff@x.com"})
print("decision:", d.basis, d.enabled)

# 非法刷新：类型错误 + 未知项
try:
    svc.refresh({"db": {"port": "not-a-port"}, "bogus": 1})
except ConfigError as exc:
    print("refresh rejected, still v", svc.current_version())
    for i in exc.explain():
        print("  -", i)

# 合法刷新：打开灰度到 50%
v2 = svc.refresh({"feature_flags": {"new_checkout": {"type": "bool", "default": False, "percentage": 50}}})
print("refreshed to v", v2)

# 回滚
v3 = svc.rollback(1, "灰度异常，回退")
print("rolled back to v", v3, "history:", svc.history_versions())
print(json.dumps(svc.audit_entries(), ensure_ascii=False, indent=2))
