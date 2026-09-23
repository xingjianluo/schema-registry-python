# 本地 Schema 注册中心（Python / FastAPI）

用本地存储管理**主体（subject）、版本（version）与兼容性策略（compatibility）**，
为不同调用方在并发注册与策略调整后提供**一致且可解释**的 Schema 演进结论。

## 能力总览

| 能力 | 说明 |
| --- | --- |
| 注册与版本管理 | `POST /subjects/{subject}/versions`，版本号在主体维度连续递增、不重复；相同 Schema（规范化指纹相同）幂等返回既有版本 |
| 兼容性校验 | 注册时按**当前生效策略**与最新版本比对，不兼容则以 `409 SCHEMA_INCOMPATIBLE` 拒绝，响应中逐项给出冲突类别、字段路径与细节 |
| 兼容性预检 | `POST /compatibility/subjects/{subject}/versions`，**只读**，不写入版本、不改策略，返回全部冲突来源 |
| 策略管理与历史 | 主体可单独设置策略，未设置则继承全局默认；策略变更**只影响之后的注册**，历史版本固化 `policy_snapshot` |
| 历史结论复现 | `GET /subjects/{subject}/versions/{v}/verdict` 默认按该版本登记时的策略复现，也可用 `?compatibility=` 指定策略重算 |
| 版本化缓存 | 预检结论按「候选指纹 + 基线版本 + 策略级别 + 策略修订号」缓存；策略更新或新版本登记后缓存自动失效，不会与最新登记版本矛盾 |
| 本地持久化 | JSON 文件原子落盘（tmp + `os.replace`），重启后版本与策略完整恢复 |
| 审计日志 | 注册、预检、拒绝、策略变更、复现均输出单行 JSON 日志，包含**输入、判定依据与最终结论** |

## 数据模型

Schema 采用 **Avro 风格的 JSON record** 定义（支持 record 嵌套、enum、union 及原始类型）：

```json
{
  "type": "record",
  "name": "User",
  "fields": [
    {"name": "id", "type": "long"},
    {"name": "email", "type": ["null", "string"], "default": null}
  ]
}
```

- 支持原始类型：`null boolean int long float double bytes string`。
- 类型提升（reader 读取 writer 方向）：`int→long→float→double`、`bytes↔string`（按 Avro 规则）。
- 指纹：Schema 递归按键排序后做 sha256，取前 16 位作为版本 `id`。

## 兼容性规则

| 策略 | 语义 | 允许的演进 |
| --- | --- | --- |
| `BACKWARD`（默认） | 新 schema 能读旧数据（reader=new） | 删字段、新增**带 default** 的字段、沿提升矩阵改类型、enum 新增符号 |
| `FORWARD` | 旧 schema 能读新数据（reader=old） | 增字段、沿反方向合法的类型变化、enum 删除符号 |
| `FULL` | 两个方向都兼容 | 上述两方向的交集（新增/删除字段都必须带 default） |
| `NONE` | 不校验 | 任意变更 |

首个版本在任何策略下均直接登记；每次只与**最新已登记版本**比对。

## 错误分类（原因可区分）

错误响应统一形如 `{"error_code", "message", "detail"}`，不兼容时
`detail.report.incompatibilities` 是一个数组，每项含 `code / path / message / detail`：

| code | HTTP | 含义 | 典型触发 |
| --- | --- | --- | --- |
| `SCHEMA_INVALID` | 422 | Schema 结构非法 | 缺 `fields`、字段重名、未知类型、enum default 非法 |
| `REQUIRED_FIELD_ADDED` | 409 | 新增了无 default 的必填字段（新读旧失败） | BACKWARD/FULL 下加必填字段 |
| `FIELD_REMOVED` | 409 | 新 schema 删除了字段（旧读新拿不到） | FORWARD/FULL 下删字段 |
| `REQUIRED_FIELD_REMOVED` | 409 | 被删除字段在旧 schema 中也没有 default | FORWARD/FULL 下删必填字段（与 `FIELD_REMOVED` 同时出现） |
| `FIELD_TYPE_CHANGED` | 409 | 字段类型变化且不在提升矩阵内 | `int→boolean`、record/原始类型互换 |
| `ENUM_SYMBOL_REMOVED` | 409 | reader enum 缺少 writer 仍使用的符号 | BACKWARD 下删 enum 符号 |
| `ENUM_SYMBOL_ADDED` | 409 | 新数据可能出现旧 reader 不认识的符号 | FORWARD 下加 enum 符号 |
| `SUBJECT_NOT_FOUND` / `VERSION_NOT_FOUND` | 404 | 主体或版本不存在 | 读取未登记主体 |
| `INVALID_POLICY` | 422 | 策略名非法 | 设置成四种级别以外的值 |

`path` 精确定位冲突字段，嵌套 record 形如 `$.addr.zip`。

## 并发一致性

- 每个主体一把可重入锁：同一主体的注册串行化，**版本号连续不重复、不跳号**；不同主体可并行。
- 注册流程：校验结构 → 指纹查重（幂等）→ 按当前策略校验最新版本 → 分配 `len(versions)+1` → 落盘 → 失效该主体缓存。
- 不兼容的注册在落版本前抛出异常，多线程竞争时只有兼容候选能拿到下一个版本号。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/subjects` | 主体列表 |
| POST | `/subjects/{subject}/versions` | 注册（body：`{"schema": {...}}`） |
| POST | `/compatibility/subjects/{subject}/versions` | 只读预检 |
| GET | `/subjects/{subject}/versions[/{v}]` | 版本列表 / 指定版本 |
| GET/PUT | `/subjects/{subject}/policy` | 读取 / 设置主体策略（body：`{"compatibility": "FULL"}`） |
| GET/PUT | `/config` | 读取 / 设置全局默认策略 |
| GET | `/subjects/{subject}/versions/{v}/verdict?compatibility=` | 历史结论复现（可选指定策略） |

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SCHEMA_REGISTRY_DB` | `data/registry.json` | 本地 JSON 存储路径 |
| `SCHEMA_REGISTRY_DEFAULT_POLICY` | `BACKWARD` | 初始化时的全局默认兼容性策略 |

## 运行与验证

```bash
uv sync
uv run uvicorn main:app --reload            # 启动服务
uv run pytest -s                            # 全量测试（-s 可看到输入/依据/结论审计日志）
uv run pytest tests/test_registry.py -k concurrent -s   # 只看并发场景
```

手工验证示例：

```bash
curl -s -X POST localhost:8000/subjects/users/versions \
  -H 'content-type: application/json' \
  -d '{"schema":{"type":"record","name":"User","fields":[{"name":"id","type":"int"}]}}'

curl -s -X POST localhost:8000/compatibility/subjects/users/versions \
  -H 'content-type: application/json' \
  -d '{"schema":{"type":"record","name":"User","fields":[{"name":"id","type":"string"}]}}'
# => report.compatible=false，incompatibilities[0].code=FIELD_TYPE_CHANGED
```

## 测试覆盖

- `tests/test_checker.py`：七类冲突的分类与路径、enum 方向性、嵌套 record、提升矩阵、NONE。
- `tests/test_registry.py`：版本连续/幂等、拒绝不改状态、预检只读、错误归类、8 线程并发注册、
  并发下兼容/不兼容候选竞争、策略变更后按历史快照复现、全局策略继承、重启持久化、
  策略/新版本登记后缓存结论与最新状态一致、审计日志字段完整性。
- `tests/test_api.py`：HTTP 端到端（注册、预检、策略、复现、错误码）。

## 代码结构

```
app/models.py   # 领域模型、错误分类与错误码
app/checker.py  # Schema 校验、指纹、BACKWARD/FORWARD/FULL 判定（冲突分类）
app/store.py    # 线程安全的本地 JSON 存储与主体锁
app/audit.py    # 结构化审计日志（logging + 内存流）
app/registry.py # 注册中心：注册/预检/策略/复现/版本化缓存
app/api.py      # FastAPI 路由与错误处理
main.py         # 组装入口
```
