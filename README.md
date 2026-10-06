# 运行时配置与功能开关服务

基于 FastAPI 的运行时配置管理与功能开关服务。核心能力：

- 多来源配置合并（defaults < file < env），非法配置整体拒绝，绝不静默兜底
- 开关规则求值：多属性条件 + 百分比灰度，结论确定且可解释
- **组合关系**：开关依赖（depends_on）与互斥组（exclusive_group）
- **临时覆盖**：带有效期的强制开/关，到期自动恢复
- **发布前预检**：变更摘要 + 合法性校验，不过则不允许发布
- 原子快照替换、在线刷新、回滚与审计

## 组合关系语义

在开关声明中新增两个可选字段（缺省不写即旧语义，完全向后兼容）：

```json
{
  "flags": {
    "base_ability": {"type": "bool", "default": true},
    "new_feature":  {"type": "bool", "default": true, "depends_on": ["base_ability"]},
    "ui_v2":        {"type": "bool", "default": true, "exclusive_group": "ui"},
    "ui_legacy":    {"type": "bool", "default": false, "exclusive_group": "ui"}
  }
}
```

- **depends_on**：依赖的开关未生效（关闭/缺失/被排除/值为假）时，本开关强制不生效，
  判定为 `dependency_unsatisfied`，返回自身默认值。依赖关系可传递（A→B→C）。
- **exclusive_group**：同组开关同一时刻至多一个生效。按规则求值后有多个成员生效时，
  **开关名字典序最小者胜出**，其余判定为 `group_excluded` 并回退默认值。

### 确定性保证

- 裁决只依赖开关名（排序后），与声明顺序、并发读取交错无关；
  同一请求内对同一开关的判定一致（请求级备忘）。
- 百分比分桶使用 sha256（跨进程稳定），不依赖 Python 内置 hash。

### 发布前校验（拒绝即整体不生效）

以下问题会在预检时被**明确拒绝**，并指出具体开关与关系，线上继续沿用上一份有效配置：

- `depends_on` 引用了不存在的开关；
- 依赖关系成环（含自依赖），拒绝记录给出环路径，如 `a -> b -> c -> a`；
- 互斥组内有两个及以上"无条件生效"（启用 + 无规则 + 默认值真）的开关，即静态必然冲突。

运行期按规则动态重叠的互斥成员不做硬拒绝，由字典序裁决保证确定结果。

## 临时覆盖与到期规则

线上出问题时可强制某个开关开/关一段时间：

```bash
curl -X POST localhost:8000/overrides -H 'content-type: application/json' -d '{
  "flag": "checkout", "value": false, "ttl_seconds": 600,
  "actor": "ops", "reason": "线上止血"
}'
```

- 覆盖生效期间优先级**最高**：高于规则求值、高于开关关闭状态，判定为 `override`；
- 到期后自动恢复规则判定。到期判断基于**可注入时钟**
  （`OverrideManager(clock=...)`），本地与单测可确定性验证；
- 到期采用惰性清理：读取时发现过期即记录 `expired` 事件并移除，不依赖后台线程；
- 生效（`set`）、到期（`expired`）、撤销（`revoked`）均记录事件，
  `GET /overrides` 可查；敏感开关的覆盖值在对外展示时脱敏为 `***`；
- 提前撤销：`DELETE /overrides/{flag}?actor=ops`。

## 发布前预检

刷新前可先预检，拿到本次会新增/删除/改动哪些开关与配置项的摘要：

```bash
curl -X POST localhost:8000/preflight -H 'content-type: application/json' -d '{
  "flags": {"new_flag": {"type": "bool", "default": true}}
}'
# -> {"ok": true, "summary": "开关[+1 -0 ~0] 配置项[+0 -0 ~0] 拒绝 0 条", ...}
```

- 预检是**纯查询**，不触碰线上状态；`ok=false` 时 `POST /refresh` 会被拒绝，
  线上版本、快照、审计均不变（审计仅追加一条 `refresh_rejected`）；
- 预检通过后的生效动作是**整体替换**：读请求要么看到旧的完整快照、
  要么看到新的完整快照，绝不出现新旧混杂；
- 摘要中敏感配置项的值脱敏；开关数量到上千条时求值仍为 O(规则数) 查表，
  读路径无锁，连续并发求值不随条数增长明显退化。

## 本地验证与回退

```bash
# 跑全部单测（日志含输入、命中判定与生效来源）
uv run pytest tests/ -v --log-cli-level=INFO

# 本地启动（启动期配置非法会拒绝启动并打印原因）
uv run uvicorn main:app --reload

# 配置文件方式（可选）：APP_* 环境变量优先于文件
echo '{"max_connections": 50, "debug": true}' > cfg.json
APP_MAX_CONNECTIONS=200 uv run python -c \
  "from app.config.loader import load_and_merge; print(load_and_merge('cfg.json'))"
```

回退：

- 运行中回滚：`POST /rollback {"version": <历史版本号>}`，
  历史版本号见 `GET /audit`；
- 覆盖类变更回退：`DELETE /overrides/{flag}` 立即撤销，或等 TTL 到期自动恢复；
- 发布被拒绝时无需回退：线上始终沿用上一份有效配置。

## 判定来源一览

求值结果 `kind` 字段标识生效来源：`override`（临时覆盖）> `flag_disabled` /
`dependency_unsatisfied` / `rule_match` / `default` / `rule_conflict`（规则求值）>
`group_excluded`（互斥裁决）。每次判定都带 `reason` 与配置 `version`，便于排查。
