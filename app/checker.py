"""Compatibility checking between an existing schema and a candidate schema.

Direction model (Avro reader/writer semantics):

* BACKWARD: the *candidate* schema reads data written with the *existing*
  schema (existing = writer, candidate = reader).  Fields may be deleted;
  newly added fields must carry a default; types must be promotable in the
  existing -> candidate direction.
* FORWARD: the *existing* schema reads data written with the *candidate*
  schema (candidate = writer, existing = reader).  Fields may be added;
  deleted fields must previously have carried a default; types must be
  promotable in the candidate -> existing direction.
* FULL: both directions must hold.
* NONE: every well-formed schema is accepted.

This module is pure: it never mutates its arguments and never stores state.
"""
from __future__ import annotations

from .models import CompatibilityLevel, CompatibilityReport, Incompatibility, IncompatibilityCode

PRIMITIVES = {"null", "boolean", "int", "long", "float", "double", "bytes", "string"}

# writer type -> set of reader types it can be promoted to (Avro promotion rules)
PROMOTIONS: dict[str, set[str]] = {
    "int": {"long", "float", "double"},
    "long": {"float", "double"},
    "float": {"double"},
    "string": {"bytes"},
}


class SchemaFormatError(ValueError):
    """A submitted schema document is not well formed."""

    code = IncompatibilityCode.SCHEMA_INVALID


def _type_name(t: object) -> str:
    if isinstance(t, str):
        return t
    if isinstance(t, dict):
        return str(t.get("type", "?"))
    return "?"


def _validate_type(t: object, path: str, errors: list[str]) -> None:
    if isinstance(t, str):
        if t not in PRIMITIVES:
            errors.append(f"{path}: unknown primitive type {t!r}")
        return
    if not isinstance(t, dict):
        errors.append(f"{path}: type must be a string or an object")
        return
    kind = t.get("type")
    if kind == "enum":
        symbols = t.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            errors.append(f"{path}: enum requires a non-empty 'symbols' list")
            return
        if not all(isinstance(s, str) for s in symbols):
            errors.append(f"{path}: enum symbols must all be strings")
        if len(set(symbols)) != len(symbols):
            errors.append(f"{path}: enum symbols must be unique")
    elif kind == "record":
        _validate_fields(t.get("fields"), f"{path}/fields", errors)
    else:
        errors.append(f"{path}: unsupported complex type {kind!r}")


def _validate_fields(fields: object, path: str, errors: list[str]) -> None:
    if not isinstance(fields, list) or not fields:
        errors.append(f"{path}: record requires a non-empty 'fields' list")
        return
    seen: set[str] = set()
    for i, f in enumerate(fields):
        fpath = f"{path}[{i}]"
        if not isinstance(f, dict):
            errors.append(f"{fpath}: field must be an object")
            continue
        name = f.get("name")
        if not isinstance(name, str) or not name:
            errors.append(f"{fpath}: field requires a non-empty 'name'")
        elif name in seen:
            errors.append(f"{fpath}: duplicate field name {name!r}")
        else:
            seen.add(name)
        if "type" not in f:
            errors.append(f"{fpath}/{name}: field requires 'type'")
        else:
            _validate_type(f["type"], f"{fpath}/{name}", errors)


def validate_schema(schema: dict) -> Exception | None:
    """Validate the raw shape of a schema. Return an error or None."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return SchemaFormatError("schema must be a JSON object")
    if schema.get("type") != "record":
        errors.append("/: top-level schema must be a record")
    if not isinstance(schema.get("name"), str) or not schema["name"]:
        errors.append("/: record requires a non-empty 'name'")
    _validate_fields(schema.get("fields"), "/fields", errors)
    if errors:
        return SchemaFormatError("; ".join(errors))
    return None


def _promotable(writer_type: str, reader_type: str) -> bool:
    return writer_type == reader_type or reader_type in PROMOTIONS.get(writer_type, set())


def _index_fields(record: dict) -> dict[str, dict]:
    return {f["name"]: f for f in record["fields"]}


def _compare_types(
    writer_type: object,
    reader_type: object,
    path: str,
    direction: str,
    issues: list[Incompatibility],
) -> None:
    """Compare types assuming data flows writer -> reader.

    *direction* is "backward" (existing -> candidate) or "forward"
    (candidate -> existing); it only selects the enum conflict category.
    """
    w_kind = _type_name(writer_type)
    r_kind = _type_name(reader_type)

    if isinstance(writer_type, str) and isinstance(reader_type, str):
        if not _promotable(writer_type, reader_type):
            issues.append(Incompatibility(
                IncompatibilityCode.FIELD_TYPE_CHANGED,
                f"type {writer_type!r} cannot be read as {reader_type!r}",
                path, {"from": writer_type, "to": reader_type},
            ))
        return

    if w_kind == "enum" and r_kind == "enum":
        reader_symbols = set(reader_type["symbols"])
        missing = [s for s in writer_type["symbols"] if s not in reader_symbols]
        if missing:
            code = (IncompatibilityCode.ENUM_SYMBOL_REMOVED if direction == "backward"
                    else IncompatibilityCode.ENUM_SYMBOL_ADDED)
            verb = "removed from candidate" if direction == "backward" else "added in candidate"
            issues.append(Incompatibility(
                code,
                f"enum symbols {missing} {verb} and cannot be resolved by the reader",
                path, {"symbols": missing},
            ))
        return

    if w_kind == "record" and r_kind == "record":
        _directional_check(writer_type, reader_type, path, direction, issues)
        return

    issues.append(Incompatibility(
        IncompatibilityCode.FIELD_TYPE_CHANGED,
        f"type kind changed from {w_kind!r} to {r_kind!r}",
        path, {"from": w_kind, "to": r_kind},
    ))


def _directional_check(
    writer: dict,
    reader: dict,
    base_path: str,
    direction: str,
    issues: list[Incompatibility],
) -> None:
    writer_fields = _index_fields(writer)
    reader_fields = _index_fields(reader)
    for name in writer_fields.keys() | reader_fields.keys():
        path = f"{base_path}/{name}" if base_path != "/" else f"/{name}"
        wf = writer_fields.get(name)
        rf = reader_fields.get(name)
        if wf is not None and rf is not None:
            _compare_types(wf["type"], rf["type"], path, direction, issues)
        elif rf is not None:
            # reader-only field: it must have a default or the reader cannot
            # fill it when reading data produced by the writer.
            if "default" not in rf:
                if direction == "backward":
                    code = IncompatibilityCode.FIELD_ADDED_REQUIRED
                    msg = f"new required field {name!r} has no default"
                else:
                    code = IncompatibilityCode.FIELD_REMOVED
                    msg = f"field {name!r} was removed and had no default"
                issues.append(Incompatibility(code, msg, path))
        # writer-only fields are simply ignored by the reader: always fine.


def check_compatibility(
    existing: dict | None,
    candidate: dict,
    level: CompatibilityLevel,
) -> CompatibilityReport:
    """Check *candidate* against *existing* under policy *level*.

    With no *existing* schema (first registration) every level accepts.
    """
    if existing is None or level is CompatibilityLevel.NONE:
        return CompatibilityReport(True, level)

    found: list[Incompatibility] = []
    if level in (CompatibilityLevel.BACKWARD, CompatibilityLevel.FULL):
        _directional_check(existing, candidate, "/", "backward", found)
    if level in (CompatibilityLevel.FORWARD, CompatibilityLevel.FULL):
        _directional_check(candidate, existing, "/", "forward", found)

    unique: dict[tuple, Incompatibility] = {}
    for issue in found:
        unique[(issue.code, issue.path, issue.message)] = issue
    return CompatibilityReport(not unique, level, tuple(unique.values()))
