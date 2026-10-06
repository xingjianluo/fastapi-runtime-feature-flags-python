# 运行时配置与功能开关服务

基于 FastAPI 的运行时配置管理与功能开关服务。配置多来源合并（defaults < file < env）、
开关按规则求值、在线刷新与回滚，并支持三块发布期能力：**开关组合关系**、
**带有效期的临时覆盖**、**发布前预检**。

## 快速开始

```bash
uv sync                 # 安装依赖
uv run pytest tests/    # 跑全部单测（日志含输入、命中判定与生效来源）
uv run python main.py   # 启动服务，默认 127.0.0.1:8000
```

## 一、开关组合关系

开关声明在原有字段基础上新增三个可选字段，**不写则完全按旧语义判定**：

```json
{
  "base": {"type": "bool", "default": true},
  "feat": {"type": "bool", "default": true, "depends_on": ["base"]},
  "v1":   {"type": "bool", "default": true, "mutex_group": "exp", "priority": 1},
  "v2":   {"type": "bool", "default": true, "mutex_group": "exp", "priority": 2}
}
```

语义：

- `depends_on`：依赖的开关全部"生效"（启用且求值为真）时，本开关才参与正常判定；
  任一依赖未生效，本开关强制关闭，判定为 `dependency_unsatisfied`。
- `mutex_group` + `priority`：同组开关同一时刻最多一个生效。多个成员同时局部生效时，
  `priority` 大者生效，其余判定为 `mutex_lost` 并强制关闭。
- 判定优先级：**临时覆盖 > 依赖检查 > 互斥裁决 > 规则/默认值**。

确定性保证：求值是纯函数，依赖按名称排序遍历、互斥裁决按 (priority, name) 取唯一胜者，
与声明顺序、并发交错无关；同一请求内基于同一份不可变快照，结论一致。

变更拒绝（整体拒绝、沿用上一份有效配置、服务不挂）：

| 场景 | 拒绝原因 |
|---|---|
| 依赖目标不存在 | `unknown_key` |
| 依赖成环（含自依赖） | `dependency_cycle`，指出环上的开关与路径 |
| 互斥组内优先级并列 | `relation_conflict`，指出并列的两个开关 |
| 互斥组成员间互相依赖 | `relation_conflict` |

拒绝记录包含具体开关名与问题关系，见 `RejectionReport`。

## 二、临时覆盖（带有效期）

线上应急时可强制某个开关开/关一段时间，到期自动恢复规则判定：

```bash
curl -X POST localhost:8000/overrides -H 'content-type: application/json' -d '{
  "flag": "feat", "value": true, "ttl_seconds": 300,
  "actor": "ops", "reason": "应急打开"
}'
curl -X DELETE localhost:8000/overrides/feat   # 提前撤销
curl localhost:8000/overrides/events           # 审计：set / expired / revoked
```

- 覆盖生效期间优先级**高于**规则判定，判定 `kind=override`；
- 到期判断基于注入的时钟（`OverrideManager(clock=...)`），单测用假时钟可确定性验证；
- 生效、到期、撤销均记入审计事件；敏感开关的覆盖值在对外展示时脱敏为 `***`。

## 三、发布前预检

```bash
curl -X POST localhost:8000/preflight -H 'content-type: application/json' -d '{
  "config": {"max_connections": 200},
  "flags": {"feat": {"type": "bool", "default": true}}
}'
# -> {"ok": true, "summary": {"flags_added": [...], "flags_removed": [...],
#      "flags_changed": [...], "config_added/removed/changed": [...]}, "report": {...}}
```

- 预检给出本次新增/删除/改动的开关与配置项摘要，**不修改任何运行时状态**；
- 预检不通过（组合关系校验失败、声明不合法）的候选不允许发布；
  `/refresh` 走同一套校验，失败即整体拒绝并记 `refresh_rejected` 审计；
- 生效是整体原子替换：读者要么看到旧的完整快照、要么看到新的完整快照，
  不会出现一次请求内新旧值混杂。

## 四、本地验证与回退

```bash
uv run pytest tests/ -v --log-cli-level=INFO   # 看每个用例的输入与判定来源
uv run pytest tests/test_flag_relations.py     # 组合关系
uv run pytest tests/test_overrides.py          # 临时覆盖
uv run pytest tests/test_preflight.py          # 预检与原子发布
```

线上回退：

```bash
curl -X POST localhost:8000/rollback/1         # 回滚到历史版本 v1
curl localhost:8000/audit                      # 查看 refresh / rollback / refresh_rejected 记录
```

代码层回退：`ConfigStore.rollback(to_version)`；被刷新拒绝时无需回退——线上状态从未改变。
