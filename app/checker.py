"""兼容性校验器：Avro 风格的 record schema 演进判定。

支持的类型：null / boolean / int / long / float / double / bytes / string、
record（可嵌套）、enum、union（按成员逐个比对）。

兼容性方向（Confluent Schema Registry 语义）：
- BACKWARD：新 schema 能读旧数据（reader=new, writer=old）。
  * 删除字段允许；新增字段必须带 default；共有字段 reader 类型必须能读 writer 类型。
- FORWARD：旧 schema 能读新数据（reader=old, writer=new）。
  * 新增字段允许；删除字段在旧 reader 中须有 default；共有字段同样要求类型可读。
- FULL：两个方向均满足。
- NONE：不校验。
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .models import (
    CompatibilityLevel,
    CompatibilityReport,
    Incompatibility,
    IncompatibilityCode,
)

PRIMITIVES = frozenset(
    {"null", "boolean", "int", "long", "float", "double", "bytes", "string"}
)

# writer 类型 -> 可安全读取它的 reader 类型（含自身）。
PROMOTIONS: dict[str, frozenset[str]] = {
    "null": frozenset({"null"}),
    "boolean": frozenset({"boolean"}),
    "int": frozenset({"int", "long", "float", "double"}),
    "long": frozenset({"long", "float", "double"}),
    "float": frozenset({"float", "double"}),
    "double": frozenset({"double"}),
    "bytes": frozenset({"bytes", "string"}),
    "string": frozenset({"string", "bytes"}),
}


def _type_name(node: Any) -> str:
    """返回类型节点的规范名：原始类型字符串 / record / enum / union。"""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        kind = node.get("type")
        if kind in ("record", "enum"):
            return kind
        if isinstance(kind, str):
            return kind
    if isinstance(node, list):
        return "union"
    return "unknown"


def canonicalize(schema: dict[str, Any]) -> dict[str, Any]:
    """递归按键排序，得到稳定的 JSON 形态（不改变语义）。"""
    if isinstance(schema, dict):
        return {k: canonicalize(v) for k, v in sorted(schema.items())}
    if isinstance(schema, list):
        return [canonicalize(v) for v in schema]
    return copy.deepcopy(schema)


def fingerprint(schema: dict[str, Any]) -> str:
    payload = json.dumps(canonicalize(schema), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def validate_schema(schema: Any, path: str = "$") -> list[Incompatibility]:
    """校验 Schema 结构是否合法。"""
    issues: list[Incompatibility] = []
    if not isinstance(schema, dict):
        issues.append(
            Incompatibility(
                IncompatibilityCode.SCHEMA_INVALID,
                path,
                "schema 必须是 JSON 对象",
                {"actual_type": type(schema).__name__},
            )
        )
        return issues
    if schema.get("type") != "record":
        issues.append(
            Incompatibility(
                IncompatibilityCode.SCHEMA_INVALID,
                path,
                "顶层 type 必须为 record",
                {"actual": schema.get("type")},
            )
        )
        return issues
    if not isinstance(schema.get("name"), str) or not schema["name"]:
        issues.append(
            Incompatibility(
                IncompatibilityCode.SCHEMA_INVALID,
                f"{path}.name",
                "record 必须包含非空字符串 name",
            )
        )
    fields = schema.get("fields")
    if not isinstance(fields, list):
        issues.append(
            Incompatibility(
                IncompatibilityCode.SCHEMA_INVALID,
                f"{path}.fields",
                "record 必须包含 fields 数组",
            )
        )
        return issues
    seen: set[str] = set()
    for index, field in enumerate(fields):
        fpath = f"{path}.fields[{index}]"
        if not isinstance(field, dict):
            issues.append(
                Incompatibility(
                    IncompatibilityCode.SCHEMA_INVALID, fpath, "field 必须是对象"
                )
            )
            continue
        fname = field.get("name")
        if not isinstance(fname, str) or not fname:
            issues.append(
                Incompatibility(
                    IncompatibilityCode.SCHEMA_INVALID,
                    f"{fpath}.name",
                    "field 必须包含非空字符串 name",
                )
            )
        elif fname in seen:
            issues.append(
                Incompatibility(
                    IncompatibilityCode.SCHEMA_INVALID,
                    f"{fpath}.name",
                    f"字段名重复: {fname}",
                )
            )
        else:
            seen.add(fname)
        ftype = field.get("type")
        if ftype is None:
            issues.append(
                Incompatibility(
                    IncompatibilityCode.SCHEMA_INVALID,
                    f"{fpath}.type",
                    f"字段 {fname!r} 缺少 type",
                )
            )
        else:
            issues.extend(_validate_type(ftype, f"{fpath}.type"))
        if "default" in field and ftype is not None:
            issues.extend(_validate_default(ftype, field["default"], fpath))
    return issues


def _validate_type(node: Any, path: str) -> list[Incompatibility]:
    issues: list[Incompatibility] = []
    if isinstance(node, str):
        if node not in PRIMITIVES:
            issues.append(
                Incompatibility(
                    IncompatibilityCode.SCHEMA_INVALID,
                    path,
                    f"未知的原始类型: {node}",
                )
            )
        return issues
    if isinstance(node, list):
        if not node:
            issues.append(
                Incompatibility(
                    IncompatibilityCode.SCHEMA_INVALID, path, "union 不能为空"
                )
            )
        for i, member in enumerate(node):
            issues.extend(_validate_type(member, f"{path}[{i}]"))
        return issues
    if isinstance(node, dict):
        kind = node.get("type")
        if kind == "record":
            issues.extend(validate_schema(node, path))
        elif kind == "enum":
            if not isinstance(node.get("name"), str) or not node["name"]:
                issues.append(
                    Incompatibility(
                        IncompatibilityCode.SCHEMA_INVALID,
                        f"{path}.name",
                        "enum 必须包含 name",
                    )
                )
            symbols = node.get("symbols")
            if not isinstance(symbols, list) or not symbols:
                issues.append(
                    Incompatibility(
                        IncompatibilityCode.SCHEMA_INVALID,
                        f"{path}.symbols",
                        "enum 必须包含非空 symbols 数组",
                    )
                )
            elif len(set(symbols)) != len(symbols):
                issues.append(
                    Incompatibility(
                        IncompatibilityCode.SCHEMA_INVALID,
                        f"{path}.symbols",
                        "enum symbols 存在重复",
                    )
                )
        elif isinstance(kind, str):
            if kind not in PRIMITIVES:
                issues.append(
                    Incompatibility(
                        IncompatibilityCode.SCHEMA_INVALID,
                        path,
                        f"不支持的类型定义: {kind}",
                    )
                )
        else:
            issues.append(
                Incompatibility(
                    IncompatibilityCode.SCHEMA_INVALID, path, "类型定义缺少 type"
                )
            )
        return issues
    issues.append(
        Incompatibility(
            IncompatibilityCode.SCHEMA_INVALID, path, "类型节点必须是字符串、数组或对象"
        )
    )
    return issues


def _validate_default(node: Any, default: Any, path: str) -> list[Incompatibility]:
    """对 default 做宽松校验：record/enum 的默认值必须与类型相容。"""
    issues: list[Incompatibility] = []
    if isinstance(node, dict):
        kind = node.get("type")
        if kind == "enum" and default not in node.get("symbols", []):
            issues.append(
                Incompatibility(
                    IncompatibilityCode.SCHEMA_INVALID,
                    f"{path}.default",
                    f"enum 默认值 {default!r} 不在 symbols 中",
                )
            )
        elif kind == "record" and not isinstance(default, dict):
            issues.append(
                Incompatibility(
                    IncompatibilityCode.SCHEMA_INVALID,
                    f"{path}.default",
                    "record 默认值必须是对象",
                )
            )
    return issues


# ---------------------------------------------------------------- 方向比对（结构化）


def _enum_issues(
    reader: dict[str, Any], writer: dict[str, Any], path: str, *, removed_code: IncompatibilityCode
) -> list[Incompatibility]:
    """reader enum 解析 writer enum：writer 中存在而 reader 缺失的符号即冲突。"""
    issues: list[Incompatibility] = []
    for symbol in writer.get("symbols", []):
        if symbol not in reader.get("symbols", []):
            issues.append(
                Incompatibility(
                    removed_code,
                    path,
                    f"reader 缺少 writer 使用的 enum 符号 {symbol!r}",
                    {"symbol": symbol},
                )
            )
    return issues


def _types_readable(
    reader: Any,
    writer: Any,
    path: str,
    *,
    removed_code: IncompatibilityCode,
    strict_missing: bool = True,
) -> list[Incompatibility]:
    """reader 类型能否解析 writer 类型（单方向），removed_code 表示该方向的枚举符号删除类别。

    strict_missing=False 时跳过“reader 多出字段且无 default”的检查（FORWARD 方向，
    字段删除由结构化 diff 分类，避免重复/错误归类）。
    """
    issues: list[Incompatibility] = []
    r_name, w_name = _type_name(reader), _type_name(writer)

    if r_name == "record" and w_name == "record":
        issues.extend(
            _records_readable(
                reader,
                writer,
                path,
                removed_code=removed_code,
                strict_missing=strict_missing,
            )
        )
        return issues
    if r_name == "enum" and w_name == "enum":
        issues.extend(_enum_issues(reader, writer, path, removed_code=removed_code))
        return issues
    if r_name == "union":
        assert isinstance(reader, list)
        per_member = [
            _types_readable(
                member,
                writer,
                path,
                removed_code=removed_code,
                strict_missing=strict_missing,
            )
            for member in reader
        ]
        if per_member and all(per_member):
            issues.append(
                Incompatibility(
                    IncompatibilityCode.FIELD_TYPE_CHANGED,
                    path,
                    f"union reader 中没有成员可匹配 writer 类型 {w_name}",
                    {"reader": "union", "writer": w_name},
                )
            )
        return issues
    if w_name == "union":
        assert isinstance(writer, list)
        for i, member in enumerate(writer):
            issues.extend(
                _types_readable(
                    reader,
                    member,
                    f"{path}<union[{i}]>",
                    removed_code=removed_code,
                    strict_missing=strict_missing,
                )
            )
        return issues
    if r_name in PRIMITIVES and w_name in PRIMITIVES:
        if r_name not in PROMOTIONS[w_name]:
            issues.append(
                Incompatibility(
                    IncompatibilityCode.FIELD_TYPE_CHANGED,
                    path,
                    f"类型从 {w_name} 变更为 {r_name}，{r_name} 无法读取 {w_name} 数据",
                    {"from": w_name, "to": r_name},
                )
            )
        return issues
    if r_name != w_name:
        issues.append(
            Incompatibility(
                IncompatibilityCode.FIELD_TYPE_CHANGED,
                path,
                f"类型从 {w_name} 变更为 {r_name}，结构不兼容",
                {"from": w_name, "to": r_name},
            )
        )
    return issues


def _records_readable(
    reader: dict[str, Any],
    writer: dict[str, Any],
    path: str,
    *,
    removed_code: IncompatibilityCode,
    strict_missing: bool = True,
) -> list[Incompatibility]:
    """字段级：reader record 解析 writer record。

    - writer 有 / reader 无：reader 直接跳过，合法（字段删除是否报错由外层结构比较决定）。
    - reader 有 / writer 无：reader 必须持有 default，否则必填新增（仅 BACKWARD 方向）。
    - 共有字段：递归判定类型可读。
    """
    issues: list[Incompatibility] = []
    writer_fields = {f["name"]: f for f in writer.get("fields", []) if isinstance(f, dict)}
    reader_fields = {f["name"]: f for f in reader.get("fields", []) if isinstance(f, dict)}
    if strict_missing:
        for name, rfield in reader_fields.items():
            if name not in writer_fields and "default" not in rfield:
                fpath = f"{path}.{name}"
                issues.append(
                    Incompatibility(
                        IncompatibilityCode.REQUIRED_FIELD_ADDED,
                        fpath,
                        f"reader 新增必填字段 {name!r}，writer 数据缺少该字段且未提供 default",
                        {"field": name},
                    )
                )
    for name, rfield in reader_fields.items():
        wfield = writer_fields.get(name)
        if wfield is not None:
            issues.extend(
                _types_readable(
                    rfield.get("type"),
                    wfield.get("type"),
                    f"{path}.{name}",
                    removed_code=removed_code,
                    strict_missing=strict_missing,
                )
            )
    return issues


def _structural_field_diff(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    path: str,
    *,
    removed_code: IncompatibilityCode,
    require_default_on_removed: bool,
) -> list[Incompatibility]:
    """沿两个 record 的字段树递归，报告“candidate 相对 baseline 删除字段”的冲突。

    require_default_on_removed=True（FORWARD）时：
      删除任意字段报 FIELD_REMOVED；被删字段在 baseline 中无 default 再叠加 REQUIRED_FIELD_REMOVED。
    require_default_on_removed=False（BACKWARD）时删除合法，不产出冲突。
    嵌套 record 同样递归。
    """
    issues: list[Incompatibility] = []
    old_fields = {f["name"]: f for f in baseline.get("fields", []) if isinstance(f, dict)}
    new_fields = {f["name"]: f for f in candidate.get("fields", []) if isinstance(f, dict)}
    for name, ofield in old_fields.items():
        fpath = f"{path}.{name}"
        nfield = new_fields.get(name)
        if nfield is None:
            if require_default_on_removed:
                issues.append(
                    Incompatibility(
                        IncompatibilityCode.FIELD_REMOVED,
                        fpath,
                        f"新 schema 删除了字段 {name!r}，旧 reader 无法从新数据中获得它",
                        {"field": name},
                    )
                )
                if "default" not in ofield:
                    issues.append(
                        Incompatibility(
                            IncompatibilityCode.REQUIRED_FIELD_REMOVED,
                            fpath,
                            f"被删除字段 {name!r} 在旧 schema 中为必填（无 default）",
                            {"field": name},
                        )
                    )
            continue
        otype, ntype = ofield.get("type"), nfield.get("type")
        if _type_name(otype) == "record" and _type_name(ntype) == "record":
            issues.extend(
                _structural_field_diff(
                    otype,
                    ntype,
                    fpath,
                    removed_code=removed_code,
                    require_default_on_removed=require_default_on_removed,
                )
            )
    return issues


def _backward_check(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[Incompatibility]:
    """candidate(reader) 读取 baseline(writer)。

    允许删除字段；新增必填字段拒绝；类型变更按 reader<-writer 提升判定；
    enum 只允许新增符号（删除符号 -> ENUM_SYMBOL_REMOVED）。
    """
    issues = _types_readable(
        candidate, baseline, "$", removed_code=IncompatibilityCode.ENUM_SYMBOL_REMOVED
    )
    issues.extend(
        _structural_field_diff(
            baseline,
            candidate,
            "$",
            removed_code=IncompatibilityCode.ENUM_SYMBOL_REMOVED,
            require_default_on_removed=False,
        )
    )
    return issues


def _forward_check(baseline: dict[str, Any], candidate: dict[str, Any]) -> list[Incompatibility]:
    """baseline(reader) 读取 candidate(writer)。

    允许新增字段；删除字段拒绝（FIELD_REMOVED，必填再叠加 REQUIRED_FIELD_REMOVED）；
    enum 只允许删除符号（新增符号 -> ENUM_SYMBOL_ADDED）。
    """
    issues = _types_readable(
        baseline,
        candidate,
        "$",
        removed_code=IncompatibilityCode.ENUM_SYMBOL_ADDED,
        strict_missing=False,
    )
    issues.extend(
        _structural_field_diff(
            baseline,
            candidate,
            "$",
            removed_code=IncompatibilityCode.ENUM_SYMBOL_ADDED,
            require_default_on_removed=True,
        )
    )
    return issues


def _dedup(issues: list[Incompatibility]) -> list[Incompatibility]:
    seen: set[tuple[str, str]] = set()
    result: list[Incompatibility] = []
    for item in issues:
        key = (item.code.value, item.path)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def check_compatibility(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    level: CompatibilityLevel,
) -> CompatibilityReport:
    candidate_errors = validate_schema(candidate)
    baseline_errors = validate_schema(baseline)
    errors = _dedup(candidate_errors + baseline_errors)
    if errors:
        return CompatibilityReport(
            compatible=False,
            level=level,
            baseline_fingerprint=fingerprint(baseline) if isinstance(baseline, dict) else "-",
            candidate_fingerprint=fingerprint(candidate) if isinstance(candidate, dict) else "-",
            incompatibilities=errors,
        )

    issues: list[Incompatibility] = []
    if level == CompatibilityLevel.NONE:
        issues = []
    elif level == CompatibilityLevel.BACKWARD:
        issues = _backward_check(baseline, candidate)
    elif level == CompatibilityLevel.FORWARD:
        issues = _forward_check(baseline, candidate)
    elif level == CompatibilityLevel.FULL:
        issues = _backward_check(baseline, candidate) + _forward_check(
            baseline, candidate
        )
    else:  # pragma: no cover - 防御性分支
        raise ValueError(f"未知兼容性策略: {level}")

    issues = _dedup(issues)
    return CompatibilityReport(
        compatible=not issues,
        level=level,
        baseline_fingerprint=fingerprint(baseline),
        candidate_fingerprint=fingerprint(candidate),
        incompatibilities=issues,
    )
