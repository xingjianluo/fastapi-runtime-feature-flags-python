"""服务入口：加载配置 -> 建存储 -> 起 HTTP 服务。"""
import time

import uvicorn

from app.api import create_app
from app.config.loader import load_and_merge
from app.flags.model import FlagSet
from app.flags.overrides import OverrideManager
from app.runtime.store import ConfigStore, Snapshot


def build_store() -> ConfigStore:
    config, report = load_and_merge()
    if config is None:
        raise SystemExit(f"启动配置不合法: {[r.detail for r in report.rejections]}")
    initial = Snapshot(version=1, config=config, flags=FlagSet(version=1))
    return ConfigStore(initial, overrides=OverrideManager(time.time))


app = create_app(build_store(), clock=time.time)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
