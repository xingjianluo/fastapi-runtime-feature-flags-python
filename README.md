# 运行时分层配置与功能开关（FastAPI）

一套可在运行时调整的**分层配置 + 功能开关 + 在线刷新/回滚**体系。
配置来源变化、灰度策略与在线刷新在程序化调用和 HTTP 各入口下，
都得到一致、可解释、可回滚的结论。

## 1. 分层配置与合并优先级

### 1.1 来源与优先级（高 → 低）

| 优先级 | 来源 | 标识示例 |
| --- | --- | --- |
| 1 | 在线刷新输入（管理接口/程序化 override） | `api:refresh` / `bootstrap` |
| 2 | 环境变量（前缀 `APP_`，`__` 分层） | `env:APP_DB__POOL_SIZE` |
| 3 | 配置文件（JSON/TOML） | `file:examples/config.json` |
| 4 | 代码内默认值 | `defaults` |

规则：

- 逐项取**最高优先级**来源的值；高优先级来源若提供了该项但**类型不合法**，
  直接拒绝整次加载/刷新，**不会静默回退**到低优先级默认值。
- 环境变量名映射：`APP_DB__POOL_SIZE` → `db.pool_size`（去掉前缀、小写、
  `__` → `.`）。
- 每个生效值都记录确切来源，可通过 `GET /config` 查看。

### 1.2 严格校验：不合法即拒绝，原因可区分

出现以下任一情况，整份配置被拒绝，结论按 `source + path + kind + reason`
结构化返回（绝不静默使用兜底值）：

- `missing`：必填项在所有来源都缺失；
- `type`：类型不合法（如把 `oops` 赋给 `int`，布尔只接受
  `true/false/1/0/yes/no/on/off`，JSON 解析失败等）；
- `unknown`：出现未在 `FieldSpec` 中声明的键（环境变量、文件、默认值、
  刷新输入分别检查）；
- `constraint`：违反 `min/max` 或 `enum` 约束。

一次校验会返回**全部**问题，而不是只报第一个。

### 1.3 敏感字段脱敏

- 在 `FieldSpec(..., secret=True)` 中声明的字段，`GET /config` 默认输出
  `********`；仅当显式 `?include_secrets=true`（内网排障）才返回明文。
- 刷新输入日志、审计记录中敏感字段同样脱敏；
  敏感字段自身校验失败时，错误原因只写“敏感字段，值已隐藏”，
  原始值不会进入异常文本或审计。

## 2. 功能开关与灰度语义

开关在配置文件的 `feature_flags` 段（或刷新输入同名键）中声明：

```json
{
  "feature_flags": {
    "new_checkout": {
      "type": "bool",
      "default": false,
      "percentage": 50,
      "percentage_key": "id",
      "rules": [
        {
          "name": "staff_rollout",
          "effect": "on",
          "conditions": [
            {"attribute": "email", "op": "prefix", "value": "staff@"}
          ]
        }
      ]
    }
  }
}
```

- 类型：`bool / int / float / str / json`；默认值必须与声明类型一致，
  非法声明整体拒绝。
- 判定顺序**固定**，结论确定可解释：
  1. **规则优先（first-match-wins）**：规则按声明顺序逐条求值，第一条
     “全部条件命中”的规则立即生效，后续规则全部忽略——因此规则顺序就是
     冲突优先级，“规则冲突”也有确定结论。空条件列表的规则不会自动命中。
  2. **百分比灰度**：无规则命中时计算稳定桶
     `bucket = int(sha256(f"{flag}:{identity}")[:8], 16) % 100`，
     `bucket < percentage` 命中（0 全关、100 全开，边界 49 命中/50 不命中）。
     同一 `(开关, 身份)` 永远同桶。未显式传 identity 时取
     `attributes[percentage_key]`。
  3. **默认值**：以上都不命中则使用 `default`。
- 条件操作符：`eq / ne / in / nin / prefix / regex / gte / lte`；
  属性缺失或类型不可比较视为不命中（不抛异常）。
- 每次判定返回 `enabled / value / basis / detail / bucket / version / trace`，
  `basis` 为 `rule:<name>`、`percentage` 或 `default`，`trace` 给出完整
  判定链，便于解释“为什么命中/未命中”。

## 3. 在线刷新与回滚

### 3.1 刷新协议

1. 在锁外**完整构建并校验**新配置（设置 + 全部开关声明）；
2. 校验失败：写一条 `reject` 审计，抛出 `ConfigError`，
   **继续使用最近一次有效配置**，版本不变；
3. 校验成功：在同一把锁内以“替换单个不可变快照引用”的方式原子发布，
   写 `refresh` 审计。

因此：失败刷新不会产生半应用状态；并发刷新被串行化。

### 3.2 一致性：不撕裂

- 读路径只在锁内取一次不可变快照引用，之后完全无锁。
  同一请求拿到快照后，即使期间发生刷新，也只会看到**一份完整配置**，
  不会出现部分生效或撕裂读取；快照内配置与开关判定器版本严格一致。
- 并发测试（`tests/test_store.py`）用 8 个读线程 + 4 个写线程（合法/非法
  刷新交替）验证无异常、版本号无空洞。

### 3.3 版本与回滚

- 版本号严格单调 `+1`；
- **回滚 = 内容明确回退 + 版本继续前进**：`rollback(v)` 生成新版本号，
  其内容为历史版本 v 的内容，回退目标和原因写入审计。
  版本号因此始终是可靠的全序，不会倒退或重复；
- 每次 `load / refresh / reject / rollback` 都有追加式审计记录
  （序号、UTC 时间、新旧版本、是否成功、原因、来源），且不含敏感值。

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 存活与当前版本 |
| GET | `/config?include_secrets=false` | 当前配置（默认脱敏，带来源） |
| GET | `/flags/{key}?identity=&email=&tier=` | 开关判定（query 方式） |
| POST | `/flags/{key}/decision` | 开关判定（body：`identity`、`attributes`） |
| POST | `/refresh` | 在线刷新（body：`{"config": {...}}`）；失败返回 422 与结构化 issues |
| POST | `/rollback` | 回滚（body：`{"version": 1, "reason": "..."}`） |
| GET | `/audit` | 追加式审计记录 |

## 5. 本地验证

需要 Python 3.14+ 与 [uv](https://docs.astral.sh/uv/)。

```bash
# 安装依赖
uv sync

# 运行全部测试（36 个用例；日志会打印输入配置与最终决策来源）
uv run pytest

# 端到端手动演示：加载 -> 判定 -> 非法刷新被拒 -> 合法刷新 -> 回滚
PYTHONPATH=. uv run python examples/run_demo.py

# 启动服务（默认读取 examples/config.json，可用 APP_CONFIG_PATH 覆盖）
uv run python main.py
# 然后：
curl -s http://127.0.0.1:8000/config | jq
curl -s "http://127.0.0.1:8000/flags/new_checkout?identity=u1&email=staff@x.com" | jq
curl -s -X POST http://127.0.0.1:8000/refresh \
  -H 'content-type: application/json' \
  -d '{"config":{"feature_flags":{"new_checkout":{"type":"bool","default":false,"percentage":100}}}}'
curl -s -X POST http://127.0.0.1:8000/rollback \
  -H 'content-type: application/json' -d '{"version":1,"reason":"demo"}'
curl -s http://127.0.0.1:8000/audit | jq
```

## 6. 代码结构

```
app/
  errors.py   # Issue / ConfigError：带来源与原因的拒绝结论
  models.py   # FieldSpec / Setting / MergedConfig
  merge.py    # 文件/环境加载、优先级合并、严格类型与约束校验
  flags.py    # FlagSpec/Rule/Condition、稳定分桶、可解释判定
  store.py    # 线程安全不可变快照存储、原子发布、版本与回滚
  audit.py    # 追加式、线程安全审计日志
  service.py  # 门面：构建-校验-脱敏-发布-回滚-审计
  api.py      # FastAPI 入口（与程序化调用共享同一服务）
main.py       # 启动引导
tests/        # 合并/灰度边界/并发一致性/回滚/脱敏/HTTP 测试
examples/     # 示例配置与手动演示脚本
```
