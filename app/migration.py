"""Offline export/import of registry data between local instances.

A *bundle* is a plain JSON-compatible dict capturing, per subject:

* every registered version (number, schema, schema_id, registered_at),
* the full policy history (level, effective_version, changed_at),
* per-version derived facts used for integrity checks: the policy level
  in force at registration time and the compatibility conclusion reached
  under that policy.

Import is two-phase: first the bundle is validated and a plan is computed
against a snapshot of the target (no mutation), then the plan is applied
in one short critical section.  Any failure in phase one leaves the target
untouched (nothing to roll back); phase two only performs in-memory
appends that cannot fail, so readers either see the full pre-import or
post-import state, never an intermediate one.

All functions in this module are pure: they never touch a store.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

from .checker import (
    canonical_schema_id,
    check_compatibility,
    validate_schema,
)
from .config import DEFAULT_COMPATIBILITY
from .models import (
    CompatibilityLevel,
    CompatibilityReport,
    Incompatibility,
    IncompatibilityCode,
    PolicyRecord,
    RegisteredSchema,
)

BUNDLE_FORMAT = "schema-registry-bundle"
BUNDLE_FORMAT_VERSION = 1


class ConflictMode(str, enum.Enum):
    """How import treats subjects that already exist in the target."""

    REJECT = "reject"  # any existing subject aborts the whole import
    MERGE = "merge"    # keep target versions, append only missing ones


class MigrationError(ValueError):
    """Base class for migration failures. Carries a machine-readable code."""

    code = "MIGRATION_ERROR"


class BundleMalformedError(MigrationError):
    """The bundle is not a well-formed export document (structure/type)."""

    code = "BUNDLE_MALFORMED"


class BundleInconsistentError(MigrationError):
    """The bundle is structurally fine but its contents contradict each
    other (non-dense versions, schema_id mismatch, recorded policy or
    conclusion disagrees with recomputation)."""

    code = "BUNDLE_INCONSISTENT"


class ImportConflictError(MigrationError):
    """The bundle collides with data already registered in the target."""

    code = "IMPORT_CONFLICT"


@dataclass(frozen=True, slots=True)
class SubjectPlan:
    """How one subject will be mutated by an import."""

    name: str
    # versions to append (empty when the subject is fully present already)
    new_versions: tuple[RegisteredSchema, ...] = ()
    # policy records to append to the target's history
    new_policies: tuple[PolicyRecord, ...] = ()
    # versions already present with identical content (merge mode)
    already_present: int = 0


@dataclass(frozen=True, slots=True)
class ImportSummary:
    plans: tuple[SubjectPlan, ...] = ()

    @property
    def imported_versions(self) -> int:
        return sum(len(p.new_versions) for p in self.plans)


# -- shared pure helpers -----------------------------------------------------


def policy_level_at(history: list[PolicyRecord], version: int) -> CompatibilityLevel:
    """Level in force when *version* was registered (pure replay)."""
    chosen: CompatibilityLevel | None = None
    for record in history:
        if record.effective_version <= version:
            chosen = record.level
        else:
            break
    return chosen if chosen is not None else DEFAULT_COMPATIBILITY


def _conclusion_for(versions: list[RegisteredSchema], index: int,
                    history: list[PolicyRecord]) -> tuple[CompatibilityLevel,
                                                          CompatibilityReport]:
    """Recompute the conclusion of versions[index] from raw data."""
    level = policy_level_at(history, versions[index].version)
    previous = versions[index - 1].schema if index > 0 else None
    return level, check_compatibility(previous, versions[index].schema, level)


def _report_to_dict(report: CompatibilityReport) -> dict:
    return {
        "compatible": report.compatible,
        "incompatibilities": [
            {"code": i.code.value, "message": i.message, "path": i.path,
             "detail": i.detail}
            for i in report.incompatibilities
        ],
    }


# -- export ------------------------------------------------------------------


def build_bundle(subjects: dict[str, tuple[list[RegisteredSchema],
                                           list[PolicyRecord]]],
                 exported_at: str) -> dict:
    """Serialize a store snapshot into a bundle dict.  Pure."""
    out_subjects = []
    for name in sorted(subjects):
        versions, policies = subjects[name]
        out_versions = []
        for i, record in enumerate(versions):
            level, report = _conclusion_for(versions, i, policies)
            out_versions.append({
                "version": record.version,
                "schema": record.schema,
                "schema_id": record.schema_id,
                "registered_at": record.registered_at,
                "policy": level.value,
                "conclusion": _report_to_dict(report),
            })
        out_subjects.append({
            "name": name,
            "versions": out_versions,
            "policies": [
                {"level": p.level.value,
                 "effective_version": p.effective_version,
                 "changed_at": p.changed_at}
                for p in policies
            ],
        })
    return {
        "format": BUNDLE_FORMAT,
        "format_version": BUNDLE_FORMAT_VERSION,
        "exported_at": exported_at,
        "subjects": out_subjects,
    }


# -- validation ----------------------------------------------------------------


def _malformed(msg: str) -> BundleMalformedError:
    return BundleMalformedError(msg)


def _inconsistent(msg: str) -> BundleInconsistentError:
    return BundleInconsistentError(msg)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise _malformed(msg)


def _parse_level(value: object, where: str) -> CompatibilityLevel:
    _require(isinstance(value, str), f"{where}: level must be a string")
    try:
        return CompatibilityLevel(value)
    except ValueError:
        raise _malformed(f"{where}: unknown compatibility level {value!r}")


def _parse_conclusion(raw: object, where: str) -> CompatibilityReport:
    _require(isinstance(raw, dict), f"{where}: conclusion must be an object")
    _require(isinstance(raw.get("compatible"), bool),
             f"{where}: conclusion.compatible must be a boolean")
    raw_issues = raw.get("incompatibilities")
    _require(isinstance(raw_issues, list),
             f"{where}: conclusion.incompatibilities must be a list")
    issues: list[Incompatibility] = []
    for j, item in enumerate(raw_issues):
        ipath = f"{where}.incompatibilities[{j}]"
        _require(isinstance(item, dict), f"{ipath}: must be an object")
        code_raw = item.get("code")
        _require(isinstance(code_raw, str), f"{ipath}: code must be a string")
        try:
            code = IncompatibilityCode(code_raw)
        except ValueError:
            raise _malformed(f"{ipath}: unknown code {code_raw!r}")
        _require(isinstance(item.get("message"), str),
                 f"{ipath}: message must be a string")
        _require(isinstance(item.get("path"), str),
                 f"{ipath}: path must be a string")
        detail = item.get("detail", {})
        _require(isinstance(detail, dict), f"{ipath}: detail must be an object")
        issues.append(Incompatibility(code, item["message"],
                                      item["path"], detail))
    return CompatibilityReport(raw["compatible"], CompatibilityLevel.NONE,
                               tuple(issues))


def _same_conclusion(recorded: CompatibilityReport,
                     recomputed: CompatibilityReport) -> bool:
    if recorded.compatible != recomputed.compatible:
        return False
    sig = lambda r: [(i.code.value, i.path) for i in r.incompatibilities]
    return sig(recorded) == sig(recomputed)


def validate_bundle(bundle: object) -> list[dict]:
    """Check a bundle end-to-end; return normalized subject entries.

    Each entry: {"name", "versions": [RegisteredSchema], "policies":
    [PolicyRecord]}.  Raises BundleMalformedError for structural problems
    and BundleInconsistentError for self-contradictory contents.  Pure.
    """
    _require(isinstance(bundle, dict), "bundle must be a JSON object")
    _require(bundle.get("format") == BUNDLE_FORMAT,
             f"format must be {BUNDLE_FORMAT!r}")
    _require(bundle.get("format_version") == BUNDLE_FORMAT_VERSION,
             f"unsupported format_version {bundle.get('format_version')!r}")
    raw_subjects = bundle.get("subjects")
    _require(isinstance(raw_subjects, list), "subjects must be a list")

    entries: list[dict] = []
    seen_names: set[str] = set()
    for s, raw_subject in enumerate(raw_subjects):
        where = f"subjects[{s}]"
        _require(isinstance(raw_subject, dict), f"{where}: must be an object")
        name = raw_subject.get("name")
        _require(isinstance(name, str) and name,
                 f"{where}: name must be a non-empty string")
        _require(name not in seen_names,
                 f"{where}: duplicate subject {name!r}")
        seen_names.add(name)

        raw_policies = raw_subject.get("policies")
        _require(isinstance(raw_policies, list),
                 f"{where}: policies must be a list")
        policies: list[PolicyRecord] = []
        prev_ev = 0
        for p, raw_policy in enumerate(raw_policies):
            pwhere = f"{where}.policies[{p}]"
            _require(isinstance(raw_policy, dict),
                     f"{pwhere}: must be an object")
            level = _parse_level(raw_policy.get("level"), pwhere)
            ev = raw_policy.get("effective_version")
            _require(isinstance(ev, int) and not isinstance(ev, bool) and ev >= 1,
                     f"{pwhere}: effective_version must be an integer >= 1")
            _require(isinstance(raw_policy.get("changed_at"), str),
                     f"{pwhere}: changed_at must be a string")
            if ev < prev_ev:
                raise _inconsistent(
                    f"{pwhere}: effective_version {ev} goes backwards "
                    f"(previous {prev_ev})")
            prev_ev = ev
            policies.append(PolicyRecord(level, ev, raw_policy["changed_at"]))

        raw_versions = raw_subject.get("versions")
        _require(isinstance(raw_versions, list),
                 f"{where}: versions must be a list")
        for p, policy in enumerate(policies):
            if policy.effective_version > len(raw_versions) + 1:
                raise _inconsistent(
                    f"{where}.policies[{p}]: effective_version "
                    f"{policy.effective_version} exceeds version count "
                    f"{len(raw_versions)} + 1")

        versions: list[RegisteredSchema] = []
        for v, raw_version in enumerate(raw_versions):
            vwhere = f"{where}.versions[{v}]"
            _require(isinstance(raw_version, dict),
                     f"{vwhere}: must be an object")
            number = raw_version.get("version")
            _require(isinstance(number, int) and not isinstance(number, bool),
                     f"{vwhere}: version must be an integer")
            if number != v + 1:
                raise _inconsistent(
                    f"{vwhere}: versions must be dense 1..N, got {number} "
                    f"at position {v + 1}")
            schema = raw_version.get("schema")
            _require(isinstance(schema, dict), f"{vwhere}: schema must be an object")
            schema_err = validate_schema(schema)
            if schema_err is not None:
                raise _inconsistent(f"{vwhere}: schema invalid: {schema_err}")
            schema_id = raw_version.get("schema_id")
            _require(isinstance(schema_id, str),
                     f"{vwhere}: schema_id must be a string")
            if schema_id != canonical_schema_id(schema):
                raise _inconsistent(
                    f"{vwhere}: schema_id {schema_id!r} does not match "
                    f"schema content")
            _require(isinstance(raw_version.get("registered_at"), str),
                     f"{vwhere}: registered_at must be a string")
            recorded_level = _parse_level(raw_version.get("policy"), vwhere)
            recorded = _parse_conclusion(raw_version.get("conclusion"), vwhere)
            versions.append(RegisteredSchema(
                subject=name, version=number, schema=schema,
                schema_id=schema_id,
                registered_at=raw_version["registered_at"]))
            # cross-check derived facts against recomputation
            level, report = _conclusion_for(versions, v, policies)
            if recorded_level != level:
                raise _inconsistent(
                    f"{vwhere}: recorded policy {recorded_level.value} "
                    f"contradicts policy history (recomputed {level.value})")
            if not _same_conclusion(recorded, report):
                raise _inconsistent(
                    f"{vwhere}: recorded conclusion "
                    f"compatible={recorded.compatible} contradicts "
                    f"recomputed compatible={report.compatible} under "
                    f"{level.value}")

        entries.append({"name": name, "versions": versions,
                        "policies": policies})
    return entries


# -- import planning -----------------------------------------------------------


def _same_content(a: RegisteredSchema, b: RegisteredSchema) -> bool:
    return a.schema_id == b.schema_id and a.schema == b.schema


def plan_import(entries: list[dict],
                target: dict[str, tuple[list[RegisteredSchema],
                                        list[PolicyRecord]]],
                mode: ConflictMode) -> list[SubjectPlan]:
    """Compute the per-subject plan against a target snapshot.

    Raises ImportConflictError on any collision.  Pure: *target* is only
    read.  All subjects are planned before anything is applied, so a
    conflict anywhere aborts the whole import.
    """
    plans: list[SubjectPlan] = []
    for entry in entries:
        name = entry["name"]
        versions: list[RegisteredSchema] = entry["versions"]
        policies: list[PolicyRecord] = entry["policies"]
        existing = target.get(name)
        t_versions = list(existing[0]) if existing else []
        t_policies = list(existing[1]) if existing else []

        if mode is ConflictMode.REJECT and (t_versions or t_policies):
            raise ImportConflictError(
                f"subject {name!r} already exists in target "
                f"({len(t_versions)} versions); mode=reject")

        if not t_versions and not t_policies:
            plans.append(SubjectPlan(
                name=name,
                new_versions=tuple(versions),
                new_policies=tuple(policies),
                already_present=0,
            ))
            continue

        # merge mode (or reject already handled above)
        overlap = min(len(t_versions), len(versions))
        for i in range(overlap):
            if not _same_content(t_versions[i], versions[i]):
                raise ImportConflictError(
                    f"subject {name!r} version {i + 1}: same version number "
                    f"but different content "
                    f"(target id={t_versions[i].schema_id}, "
                    f"bundle id={versions[i].schema_id})")

        if len(versions) <= len(t_versions):
            # fully present already: no version or policy changes at all
            plans.append(SubjectPlan(name=name, already_present=overlap))
            continue

        new_versions = tuple(versions[len(t_versions):])
        # Policy records that governed the appended versions must be
        # reproduced so recheck yields the bundle's conclusions.  Target
        # records never have effective_version > len(t_versions) + 1 and
        # additions never have effective_version < that, so appending
        # keeps the history sorted (bundle records win at equal
        # effective_version, which is exactly what reproduction needs).
        t = len(t_versions)
        have = {(p.level, p.effective_version) for p in t_policies}
        additions = [
            p for p in policies
            if p.effective_version > t and (p.level, p.effective_version) not in have
        ]
        # The record governing the first appended version may sit at an
        # effective_version <= t (not exported above); inject a marker so
        # policy_at(t + 1) matches the bundle even then.
        wanted = policy_level_at(policies, t + 1)
        if policy_level_at(t_policies + additions, t + 1) != wanted:
            governing = [p for p in policies if p.effective_version <= t + 1]
            changed_at = (governing[-1].changed_at if governing
                          else versions[t].registered_at)
            additions.append(PolicyRecord(wanted, t + 1, changed_at))
        plans.append(SubjectPlan(
            name=name,
            new_versions=new_versions,
            new_policies=tuple(additions),
            already_present=overlap,
        ))
    return plans
