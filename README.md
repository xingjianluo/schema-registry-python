# Schema Registry（Python 版）

基于 FastAPI 的本地 Schema 注册与演进校验服务：管理**主体（subject）**、
**版本（version）**与**兼容性策略（compatibility policy）**，在并发注册与
策略调整后，对同一份 Schema 给出一致、可复现、可解释的结论。

## 能力概览

- Schema 注册与版本管理：版本号在主体内**从 1 开始、连续、不重复**；
  被拒绝的注册**不占用**版本号。
- 注册时按**当前生效策略**校验；不兼容返回 HTTP `409`，并附带
  **可区分错误码 + 字段路径 + 详情**。
- 兼容性预检：只读，绝不登记任何状态；支持显式指定策略做假设性检查。
- 策略调整：新策略**只影响其后的注册**；历史版本可按**当时策略**复现结论。
- 结论缓存：登记新版本或切换策略后相关缓存立即失效/被判为陈旧，
  缓存结论不会与最新登记版本矛盾。
- 并发安全：注册的"检查—写入"由单把锁串行化。

## 数据模型

Schema 使用 Avro 风格的 JSON 文档，顶层必须是 `record`：

```json
{
  "type": "record",
  "name": "User",
  "fields": [
    {"name": "id",   "type": "long"},
    {"name": "name", "type": "string"},
    {"name": "nick", "type": "string", "default": ""}
  ]
}
```

支持：

- 基本类型：`null boolean int long float double bytes string`
- `enum`（`symbols` 为非空、唯一的字符串数组）
- 嵌套 `record`
- 字段 `default`（出现即视为"可选字段"）

## 兼容性规则

采用 Avro 的 reader/writer 语义，比较"候选 Schema"与"最新已登记版本"。
类型提升（promotion）遵循 Avro：

| writer 类型 | 可被读取为（reader） |
| --- | --- |
| `int` | `int`、`long`、`float`、`double` |
| `long` | `long`、`float`、`double` |
| `float` | `float`、`double` |
| `string` | `string`、`bytes` |
| 其余 | 仅同类型 |

| 策略 | 含义 | 允许 | 拒绝 |
| --- | --- | --- | --- |
| `BACKWARD`（默认） | 新 Schema 读旧数据 | 删字段；新增**带 default** 的字段；按上表提升类型 | 新增**无 default** 的必填字段；不可提升的类型变更；候选 enum 丢失旧数据可能出现的 symbol |
| `FORWARD` | 旧 Schema 读新数据 | 加字段；删除**曾带 default** 的字段 | 删除**无 default** 的字段；不可提升的类型变更；候选 enum 新增旧 reader 无法解析的 symbol |
| `FULL` | 双向兼容 | 同时满足 BACKWARD 与 FORWARD | 任一方向不满足 |
| `NONE` | 不校验 | 一切结构合法的 Schema | —— |

嵌套 record 递归比较，`path` 形如 `/addr/city`，可直接定位冲突字段。

## 错误分类

校验结论中的每个冲突都是独立条目，包含 `code` / `message` / `path` / `detail`：

| code | 触发场景 |
| --- | --- |
| `SCHEMA_INVALID` | 文档不是合法 record：缺字段、未知类型、enum symbol 重复等（HTTP 422） |
| `FIELD_ADDED_REQUIRED` | BACKWARD 下新增了没有 default 的必填字段 |
| `FIELD_REMOVED` | FORWARD 下删除了没有 default 的字段 |
| `FIELD_TYPE_CHANGED` | 类型既不相同也不可按 promotion 提升（或 record/enum 大类互变） |
| `ENUM_SYMBOL_REMOVED` | BACKWARD 下候选 enum 删除了 symbol |
| `ENUM_SYMBOL_ADDED` | FORWARD 下候选 enum 新增了 symbol |

一次注册可能同时返回多个、多类冲突（例如既有类型变更又有必填新增）。

## 配置项

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SCHEMA_REGISTRY_COMPATIBILITY` | `BACKWARD` | 新主体在未显式设置策略时使用的默认级别，取值 `NONE/BACKWARD/FORWARD/FULL` |

策略为**主体级**，且带完整历史：每次变更记录 `effective_version`
（从下一个待注册版本开始生效）。`policy_at(subject, version)` 据此还原
任意历史版本注册时的生效策略。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/subjects/{subject}/versions` | 注册 Schema，body：`{"schema": {...}}` |
| POST | `/subjects/{subject}/compatibility` | 预检（只读），可带 `"compatibility": "FORWARD"` 覆盖策略 |
| GET  | `/subjects/{subject}/versions` | 列出全部版本 |
| GET  | `/subjects/{subject}/versions/{version}` | 取指定版本 |
| GET  | `/subjects/{subject}/compatibility/policy` | 读取当前策略 |
| PUT  | `/subjects/{subject}/compatibility/policy` | 调整策略，body：`{"compatibility": "FULL"}` |
| GET  | `/subjects/{subject}/compatibility/recheck/{version}` | 按当时策略复现历史结论 |

预检/拒绝响应示例：

```json
{
  "error": "INCOMPATIBLE_SCHEMA",
  "is_compatible": false,
  "compatibility": "BACKWARD",
  "incompatibilities": [
    {"code": "FIELD_TYPE_CHANGED",
     "message": "type 'int' cannot be read as 'string'",
     "path": "/id", "detail": {"from": "int", "to": "string"}}
  ]
}
```

## 并发一致性

- 注册路径在服务层用单把可重入锁串行化"读取最新版本 → 校验 → 分配版本号"，
  因此多线程并发注册同一主体时，版本号恒为 `1..N` 连续且唯一。
- 校验失败时不会写入版本，故不会出现版本空洞。
- 策略变更与注册并发时：注册看到的是加锁瞬间的策略，策略历史
  （`effective_version` 单调）保证任何已接受版本都能用
  `recheck` 按当时策略复现。
- 缓存条目记录"计算时主体的最新版本号"，读取时若主体版本已推进则丢弃；
  注册成功与策略切换还会主动 `invalidate_subject`，双重保证不返回过期结论。

## 运行与验证

```bash
uv sync
uv run uvicorn main:app --reload          # 启动服务
uv run pytest                             # 全量测试（含输入/依据/结论日志）
```

测试覆盖（`tests/`）：

- `test_checker.py`：四种策略、字段增删、类型提升、enum、嵌套路径、错误分类
- `test_service.py`：注册拒绝不落版本、预检只读、策略只影响后续注册、
  历史按当时策略复现、缓存与新版本一致性
- `test_concurrency.py`：16 线程并发注册版本连续唯一、破坏性变更并发全拒绝、
  策略反复翻转与注册竞争后的可复现性
- `test_api.py`：HTTP 端到端、409/422/404 状态码与响应体

测试日志通过 `tests/conftest.py` 配置，每个关键用例打印
`INPUT`（输入 Schema/策略）、`BASIS`（判定依据）、`CONCLUSION`
（兼容与否及分类原因），便于审计与解释。

## 目录结构

```
app/
  models.py    领域类型：策略级别、错误码、冲突、报告、登记记录、策略记录
  checker.py   纯函数：Schema 合法性校验 + 兼容性判定
  storage.py   线程安全内存存储：版本列表 + 策略历史
  cache.py     版本感知的结论缓存
  service.py   编排：注册、预检、策略、历史复现
  api.py       FastAPI 路由与错误映射
  config.py    环境变量配置
main.py        应用入口
tests/         测试与日志配置
```

> 说明：存储为进程内内存实现，重启后数据不保留；判定逻辑（`checker.py`）
> 为纯函数，不依赖存储与 IO，可独立复用。
