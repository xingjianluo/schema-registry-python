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
- 离线迁移：导出/导入一个或一批主体的版本、策略历史与历史结论；
  支持"冲突整体拒绝"或"只合并缺失版本"，导入原子、读写不互见中间态，
  损坏/缺失/矛盾的迁移包按类别明确拒绝。

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
| POST | `/migration/export` | 离线导出（body：`{"subjects": ["a","b"]}`，省略或空对象表示全部） |
| POST | `/migration/import?mode=REJECT` | 离线导入，`mode=REJECT`（默认）或 `MERGE`，请求体为导出的二进制包 |

## 本地实例间迁移（导出 / 导入）

用于把一个或一批主体搬到**新的空实例**，或把两个实例的注册数据合并，
同时**不破坏版本顺序与历史结论**。

### 迁移包包含什么

包是一个自描述、规范序列化（key 排序）的 JSON 信封，二进制
（`application/octet-stream`）传输：

```
envelope
├─ format / package_version   格式标识与版本
├─ exported_at                导出时刻
├─ default_level              导出方实例默认策略
├─ checksum                   根校验和（覆盖除自身外的全部字段）
└─ subjects[]                 每个主体一项，各自带 checksum
   └─ body
      ├─ subject              主体名
      ├─ default_level        该主体所在实例的默认策略
      ├─ versions[]           每个版本：version、schema 全文、schema_id、
      │                        registered_at（原始登记时间原样保留）
      ├─ policies[]           完整策略历史：level、effective_version、changed_at
      └─ conclusions[]        每个版本登记时的历史兼容结论：
                               version、当时 level、compatible、
                               incompatibility_codes（分类码列表）
```

**只导版本不导策略/结论不算合格**：策略历史与每个版本的历史结论都是包的
必备部分。采用两层 SHA-256 校验和（每主体一层 + 根一层）：篡改、截断、
只重算根校验都能被发现。

导入到全新实例时，若源主体从未显式设置过策略（一直使用实例默认策略），
会把该默认策略**物化成一条显式的 v1 策略记录**，因此即便目标实例的默认
策略与源不同，`recheck` 仍能按源当时的策略复现历史结论。

### 冲突与合并规则

| 情况 | `REJECT`（默认） | `MERGE` |
| --- | --- | --- |
| 目标无此主体 | 整体创建（版本+策略+结论） | 整体创建 |
| 同名、版本号相同且**内容相同** | 拒绝（纯同名碰撞，错误信息标明“内容相同”） | 跳过，视为已存在 |
| 同名、版本号相同但**内容不同** | 拒绝（内容冲突） | 拒绝（内容冲突） |
| 目标只有前缀、来源有后续版本 | 拒绝 | 仅**追加缺失版本**，保留目标已有版本 |
| 目标版本比来源更多 | 拒绝 | 不做任何写入 |

MERGE 追加时：

- 版本号沿用源坐标，因此始终是 `1..N` **连续、不重复、不跳号**；同一份包
  重复导入是幂等的，不会产生重复版本。
- 共享前缀区间保留**目标自己的策略历史**；追加区间携带来源当时生效的策略，
  以保证追加版本的历史结论可按当时策略复现。
- 来源中“尚未作用于任何已导出版本”的**待生效策略**（`effective_version`
  超出最后一个版本）不会带过去——它是来源对自身未来注册的意图，不应越权
  覆盖目标。导入后当前策略停留在最后一个真正生效过的策略。

### 原子性与并发

- 导入**先全量校验（解析、校验和、结构、结论重算）后一次性提交**；任何一步
  失败都整体回滚，不会留下导入到一半的状态，也**不会改动未被迁移的主体**。
- 导入提交与“注册的检查—写入”共用同一把服务级锁，因此导入与注册/预检/策略
  调整并发时，其他调用方要么看到迁移前完整状态，要么看到迁移后完整状态，
  **读不到中间态**。读操作在存储锁内取快照副本，大主体迁移不会让既有读取
  长时间不可用。
- 导入成功会使被迁移主体的预检缓存失效并校正最新版本号，缓存结论不会与
  导入后的版本矛盾。

### 异常迁移包：明确区分问题类别

| 错误 `error` | HTTP | 含义 |
| --- | --- | --- |
| `PACKAGE_CORRUPTED` | 422 | 非 JSON/截断、格式标识或版本不符、根或主体校验和不匹配 |
| `PACKAGE_CONTENT_MISSING` | 422 | 缺少版本 schema 全文、缺策略历史或缺历史结论 |
| `PACKAGE_INCONSISTENT` | 422 | 可解析但自相矛盾：版本号不连续、schema_id 与内容不符、schema 非法、未知策略级别、策略 effective_version 越界、结论策略与策略历史不一致、结论无法按相邻版本+当时策略重算、包内主体重复 |
| `IMPORT_CONFLICT` | 409 | 同名主体碰撞（REJECT）或同版本号内容不同（REJECT/MERGE） |

所有异常都在写入前抛出，绝不可能把部分数据当成功导入。

### 不支持的边界情况

- 仅支持进程内本地实例之间通过离线包迁移；不做跨网络实时复制/增量同步。
- 不自动“调和”同版本号不同内容：MERGE 不做三方合并，遇到即按冲突整体拒绝，
  需调用方先处理版本分叉。
- 不迁移实例级全局配置之外的运行态：结论缓存是可重建派生物，不打包。
- 包格式固定为 `package_version: 1`；其他版本会被直接拒绝。
- 主体命名沿用现有字符串规则；不提供主体改名/映射。

### 命令行验证示例

```bash
# 导出指定主体（也可用 {} 或省略 body 导出全部）
curl -s -X POST localhost:8000/migration/export \
  -H 'content-type: application/json' \
  -d '{"subjects": ["user"]}' --output user.mig.json

# 导入到新实例：冲突即整体拒绝
curl -s -X POST 'localhost:8001/migration/import?mode=REJECT' \
  -H 'content-type: application/octet-stream' \
  --data-binary @user.mig.json

# 或只追加缺失版本
curl -s -X POST 'localhost:8001/migration/import?mode=MERGE' \
  -H 'content-type: application/octet-stream' \
  --data-binary @user.mig.json
```

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
- `test_migration.py`：导出/导入往返一致（版本内容、schema_id、时间戳、策略
  历史、每版本历史结论）、子集/全量导出、REJECT/MERGE 规则、重复导入幂等、
  版本不对称与策略坐标重写、多主体批次原子性、未迁移主体不被改动
- `test_migration_errors.py`：损坏/缺失/矛盾三类异常包逐类区分、两层校验和、
  失败零写入
- `test_migration_concurrency.py`：大主体导入期间读不到中间态、导入与同主体
  /异主体注册竞争、导出与策略翻转竞争仍自洽、导入后预检缓存正确失效
- `test_api.py`：HTTP 端到端、409/422/404 状态码与响应体、迁移端点
  （导出/导入/冲突/合并/损坏包/非法 mode）

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
  service.py   编排：注册、预检、策略、历史复现、导出/导入
  migration.py 离线迁移包：编码/解码、两层校验和、严格校验与结论复核
  api.py       FastAPI 路由与错误映射
  config.py    环境变量配置
main.py        应用入口
tests/         测试与日志配置
```

> 说明：存储为进程内内存实现，重启后数据不保留；判定逻辑（`checker.py`）
> 为纯函数，不依赖存储与 IO，可独立复用。
