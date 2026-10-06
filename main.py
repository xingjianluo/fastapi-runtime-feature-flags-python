"""本地启动入口：加载配置 -> 建快照 -> 挂载 HTTP 入口。

运行：uv run uvicorn main:app --reload
"""
from app.api import create_app
from app.config.loader import load_and_merge
from app.flags.model import FlagSet
from app.runtime.overrides import OverrideManager
from app.runtime.store import ConfigStore, Snapshot

config, report = load_and_merge()
if config is None:
    # 启动期配置非法：拒绝启动，把原因打出来
    raise SystemExit(f"启动配置非法：{[r.__dict__ for r in report.rejections]}")

store = ConfigStore(Snapshot(version=1, config=config, flags=FlagSet(version=1)))
app = create_app(store, overrides=OverrideManager())
