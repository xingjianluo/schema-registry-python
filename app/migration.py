"""Offline export/import of registry data between local instances.

A *bundle* is a plain JSON-compatible dict capturing, per subject:

* every registered version (number, schema, schema_id, registered_at),
* the full policy history (level, effective_version, changed_at), always
  including an explicit baseline record at effective_version=1, so the
  history is self-contained and never relies on the importer's own
  default compatibility setting,
* per-version derived facts used for integrity checks: the policy level
  in force at registration time and the compatibility conclusion reached
  under that policy.

The whole payload additionally carries a SHA-256 *integrity digest*; any
truncation or byte-level tampering is detected before any validation or
write happens.

Import is two-phase: first the bundle is authenticated, validated and a
plan is computed against a snapshot of the target (no mutation), then the
plan is applied in one short critical section.  Any failure in phase one
leaves the target untouched (nothing to roll back); phase two only
performs in-memory appends that cannot fail, so readers either see the
full pre-import or post-import state, never an intermediate one.

All functions in this module are pure: they never touch a store.
"""
from __future__ import annotations

import enum
import hashlib
import json
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
BUNDLE_FORMAT_VERSION = 2
INTEGRITY_ALG = "SHA-256"
INTEGRITY_KEY = "integrity"


class ConflictMode(str, enum.Enum):
    """How import treats subjects that already exist in the target."""

    REJECT = "reject"  # any existing subject aborts the whole import
    MERGE = "merge"    # keep target versions, append only missing ones


class MigrationError(ValueError):
    """Base class for migration failures. Carries a machine-readable code."""

    code = "MIGRATION_ERROR"


class BundleCorruptError(MigrationError):
    """The bundle bytes/document itself cannot be trusted: unparseable or
    truncated JSON, missing integrity section, unknown digest algorithm
    or a digest mismatch (content was modified after export).

    Remedy: re-export / re-transfer the bundle; retrying the same bytes
    cannot succeed.
    """

    code = "BUNDLE_CORRUPT"


class BundleMalformedError(MigrationError):
    """The bundle parses and authenticates, but its envelope has the wrong
    shape: not an object, wrong format/format_version, or a field has the
    wrong type / unknown enum value.

    Remedy: the producing or transporting code is broken; investigate
    manually and re-export.
    """

    code = "BUNDLE_MALFORMED"


class BundleIncompleteError(MigrationError):
    """The bundle is structurally fine, but required content is missing:
    a version without schema/policy/conclusion, a policy record without
    level, or a subject carrying no versions and no policies.

    Remedy: the export cut lacked data; complete the source data and re-export.
    """

    code = "BUNDLE_INCOMPLETE"


class BundleInconsistentError(MigrationError):
    """The bundle is structurally fine and complete, but its contents
    contradict each other (non-dense versions, schema_id mismatch,
    recorded policy or conclusion disagrees with recomputation)."""

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


# -- integrity (tamper evidence) -----------------------------------------------


def _canonical_blob(payload: dict) -> bytes:
    """Deterministic serialization of everything *except* the signature.

    JSON round-tripping leaves the canonical blob byte-identical because
    separators/key order are fixed, so a bundle transported over HTTP or
    through a file authenticates the same as the in-memory one.
    """
    body = {k: v for k, v in payload.items() if k != INTEGRITY_KEY}
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def compute_integrity(payload: dict) -> dict:
    """Compute the integrity section for an (unsigned) bundle payload."""
    digest = hashlib.sha256(_canonical_blob(payload)).hexdigest()
    return {"alg": INTEGRITY_ALG, "sha256": digest}


def verify_integrity(bundle: object) -> None:
    """Authenticate the bundle before anything trusts its contents.

    Raises BundleCorruptError when the integrity section is missing or
    malformed, the algorithm is unknown, or the digest does not match
    (content modified, or blob truncated/re-assembled).
    """
    if not isinstance(bundle, dict):
        raise BundleCorruptError("bundle must be a JSON object")
    integrity = bundle.get(INTEGRITY_KEY)
    if integrity is None:
        raise BundleCorruptError(
            "bundle has no integrity section; refusing an unsigned bundle "
            "(re-export from a current instance)")
    if not isinstance(integrity, dict):
        raise BundleCorruptError("integrity section must be an object")
    alg = integrity.get("alg")
    if alg != INTEGRITY_ALG:
        raise BundleCorruptError(
            f"unsupported integrity algorithm {alg!r}; expected "
            f"{INTEGRITY_ALG!r}")
    recorded = integrity.get("sha256")
    if not isinstance(recorded, str):
        raise BundleCorruptError("integrity.sha256 must be a hex string")
    expected = hashlib.sha256(_canonical_blob(bundle)).hexdigest()
    if not _const_time_eq(recorded, expected):
        raise BundleCorruptError(
            "integrity digest mismatch: bundle content was modified, "
            "truncated or re-assembled after export")


def _const_time_eq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a.encode("ascii", "ignore"), b.encode("ascii", "ignore")):
        result |= x ^ y
    return result == 0


def parse_bundle_bytes(data: str | bytes) -> object:
    """Parse a raw bundle document, classifying parse failures as corrupt.

    Covers truncated JSON (cut-off files/HTTP bodies) and syntactically
    invalid documents; callers then run verify_integrity + validate_bundle.
    """
    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BundleCorruptError(
                f"bundle is not valid UTF-8: {exc}") from exc
    elif isinstance(data, str):
        text = data
    else:
        raise BundleCorruptError("bundle bytes must be str or bytes")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BundleCorruptError(
            f"bundle is not parseable JSON ({exc.msg} at line "
            f"{exc.lineno} column {exc.colno}); likely truncated or corrupted "
            f"in transit") from exc


# -- export ------------------------------------------------------------------


def _normalize_export_policies(
    versions: list[RegisteredSchema],
    policies: list[PolicyRecord],
    default_level: CompatibilityLevel,
) -> list[PolicyRecord]:
    """Make the policy history self-contained.

    The store only records *explicit* policy changes; the level governing
    the early versions is otherwise whatever the instance's default was.
    Export that fact explicitly as a baseline record at
    effective_version=1, so an importer configured with a different
    default reproduces the source's per-version policies exactly.
    """
    if policies and policies[0].effective_version == 1:
        return list(policies)
    if versions:
        changed_at = versions[0].registered_at
    elif policies:
        changed_at = policies[0].changed_at
    else:
        return []
    return [PolicyRecord(default_level, 1, changed_at), *policies]


def build_bundle(subjects: dict[str, tuple[list[RegisteredSchema],
                                           list[PolicyRecord]]],
                 exported_at: str,
                 default_level: CompatibilityLevel = DEFAULT_COMPATIBILITY) -> dict:
    """Serialize a store snapshot into a signed bundle dict.  Pure."""
    out_subjects = []
    for name in sorted(subjects):
        versions, raw_policies = subjects[name]
        policies = _normalize_export_policies(versions, raw_policies,
                                              default_level)
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
    bundle = {
        "format": BUNDLE_FORMAT,
        "format_version": BUNDLE_FORMAT_VERSION,
        "exported_at": exported_at,
        "subjects": out_subjects,
    }
    # Sign last: covers every field above.  Any later edit invalidates it.
    bundle[INTEGRITY_KEY] = compute_integrity(bundle)
    return bundle


# -- validation ----------------------------------------------------------------


def _malformed(msg: str) -> BundleMalformedError:
    return BundleMalformedError(msg)


def _incomplete(msg: str) -> BundleIncompleteError:
    return BundleIncompleteError(msg)


def _inconsistent(msg: str) -> BundleInconsistentError:
    return BundleInconsistentError(msg)


def _require(cond: bool, msg: str) -> None:
    """Type/shape assertions: a present value of the wrong kind."""
    if not cond:
        raise _malformed(msg)


def _required(obj: dict, key: str, where: str) -> object:
    """Fetch a mandatory content field; absence means incomplete data."""
    if key not in obj:
        raise _incomplete(f"{where}: required field {key!r} is missing")
    return obj[key]


def _parse_level(value: object, where: str) -> CompatibilityLevel:
    _require(isinstance(value, str), f"{where}: level must be a string")
    try:
        return CompatibilityLevel(value)
    except ValueError:
        raise _malformed(f"{where}: unknown compatibility level {value!r}")


def _parse_conclusion(raw: object, where: str) -> CompatibilityReport:
    _require(isinstance(raw, dict), f"{where}: conclusion must be an object")
    compatible = _required(raw, "compatible", where)
    _require(isinstance(compatible, bool),
             f"{where}: conclusion.compatible must be a boolean")
    raw_issues = _required(raw, "incompatibilities", where)
    _require(isinstance(raw_issues, list),
             f"{where}: conclusion.incompatibilities must be a list")
    issues: list[Incompatibility] = []
    for j, item in enumerate(raw_issues):
        ipath = f"{where}.incompatibilities[{j}]"
        _require(isinstance(item, dict), f"{ipath}: must be an object")
        code_raw = _required(item, "code", ipath)
        _require(isinstance(code_raw, str), f"{ipath}: code must be a string")
        try:
            code = IncompatibilityCode(code_raw)
        except ValueError:
            raise _malformed(f"{ipath}: unknown code {code_raw!r}")
        message = _required(item, "message", ipath)
        _require(isinstance(message, str),
                 f"{ipath}: message must be a string")
        path = _required(item, "path", ipath)
        _require(isinstance(path, str), f"{ipath}: path must be a string")
        detail = item.get("detail", {})
        _require(isinstance(detail, dict), f"{ipath}: detail must be an object")
        issues.append(Incompatibility(code, message, path, detail))
    return CompatibilityReport(compatible, CompatibilityLevel.NONE,
                               tuple(issues))


def _same_conclusion(recorded: CompatibilityReport,
                     recomputed: CompatibilityReport) -> bool:
    if recorded.compatible != recomputed.compatible:
        return False
    sig = lambda r: [(i.code.value, i.path) for i in r.incompatibilities]
    return sig(recorded) == sig(recomputed)


def validate_bundle(bundle: object) -> list[dict]:
    """Authenticate and check a bundle end-to-end; return normalized entries.

    Order matters: integrity first (trust nothing about a tampered or
    truncated document), then envelope shape, then per-subject content
    presence, then cross-field consistency.  Every failure class aborts
    before any store mutation:

    * BundleCorruptError     - digest missing/mismatched (handled by
      verify_integrity, also tolerating a non-object document);
    * BundleMalformedError   - wrong envelope or wrong field types;
    * BundleIncompleteError  - required content (version schema, policy
      information, ...) absent;
    * BundleInconsistentError- complete content that contradicts itself.

    Each returned entry: {"name", "versions": [RegisteredSchema],
    "policies": [PolicyRecord]}.  Pure.
    """
    # 1) authenticate bytes-to-content; a forged document gets no further.
    verify_integrity(bundle)
    assert isinstance(bundle, dict)  # guaranteed by verify_integrity

    # 2) envelope shape (structural; a broken producer/transport, not
    #    recoverable by "adding data").
    _require(bundle.get("format") == BUNDLE_FORMAT,
             f"format must be {BUNDLE_FORMAT!r}")
    _require(bundle.get("format_version") == BUNDLE_FORMAT_VERSION,
             f"unsupported format_version {bundle.get('format_version')!r}")
    _require(isinstance(bundle.get("exported_at"), str),
             "exported_at must be a string")
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
            # policy information missing => incomplete (re-export), while
            # a present-but-wrong value is malformed (broken producer).
            level = _parse_level(_required(raw_policy, "level", pwhere),
                                 pwhere)
            ev = _required(raw_policy, "effective_version", pwhere)
            _require(isinstance(ev, int) and not isinstance(ev, bool) and ev >= 1,
                     f"{pwhere}: effective_version must be an integer >= 1")
            changed_at = _required(raw_policy, "changed_at", pwhere)
            _require(isinstance(changed_at, str),
                     f"{pwhere}: changed_at must be a string")
            if ev < prev_ev:
                raise _inconsistent(
                    f"{pwhere}: effective_version {ev} goes backwards "
                    f"(previous {prev_ev})")
            prev_ev = ev
            policies.append(PolicyRecord(level, ev, changed_at))

        raw_versions = raw_subject.get("versions")
        _require(isinstance(raw_versions, list),
                 f"{where}: versions must be a list")

        # 3) content presence: an export cut with nothing for this subject
        #    cannot restore it.
        if not raw_versions and not raw_policies:
            raise _incomplete(
                f"{where}: subject {name!r} carries neither versions nor "
                f"policy history; re-export with its data")

        for p, policy in enumerate(policies):
            if policy.effective_version > len(raw_versions) + 1:
                raise _inconsistent(
                    f"{where}.policies[{p}]: effective_version "
                    f"{policy.effective_version} exceeds version count "
                    f"{len(raw_versions)} + 1")

        # The history must explicitly cover v1 so the importer never falls
        # back to its own default compatibility level.  Missing that
        # baseline is absent policy information, not a contradiction.
        if raw_versions and (
                not policies or policies[0].effective_version != 1):
            raise _incomplete(
                f"{where}: policy history has no record covering version 1; "
                f"the level governing the first registration is unknown "
                f"(re-export from a current instance)")

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
            schema = _required(raw_version, "schema", vwhere)
            _require(isinstance(schema, dict),
                     f"{vwhere}: schema must be an object")
            schema_err = validate_schema(schema)
            if schema_err is not None:
                raise _inconsistent(f"{vwhere}: schema invalid: {schema_err}")
            schema_id = _required(raw_version, "schema_id", vwhere)
            _require(isinstance(schema_id, str),
                     f"{vwhere}: schema_id must be a string")
            if schema_id != canonical_schema_id(schema):
                raise _inconsistent(
                    f"{vwhere}: schema_id {schema_id!r} does not match "
                    f"schema content")
            registered_at = _required(raw_version, "registered_at", vwhere)
            _require(isinstance(registered_at, str),
                     f"{vwhere}: registered_at must be a string")
            recorded_level = _parse_level(
                _required(raw_version, "policy", vwhere), vwhere)
            recorded = _parse_conclusion(
                _required(raw_version, "conclusion", vwhere), vwhere)
            versions.append(RegisteredSchema(
                subject=name, version=number, schema=schema,
                schema_id=schema_id, registered_at=registered_at))
            # 4) cross-check derived facts against recomputation from the
            #    self-contained version sequence + policy history.
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
        t = len(t_versions)
        # Build the policy history needed to reproduce the bundle's
        # conclusions for the *appended* versions without rewriting any
        # fact the target already recorded for its own versions.
        #
        # Target records keep precedence (the target is the source of
        # truth for versions it already held).  Bundle records are added
        # only where the target has no explicit record at that
        # effective_version.  Two kinds are needed:
        #   * records with effective_version > t, which govern appended
        #     versions outright;
        #   * the baseline/history at effective_version <= t when the
        #     target has no explicit records there (e.g. it relied on its
        #     own configured default).  Without it, the first appended
        #     version's governing level would silently fall back to the
        #     importer's default, which may differ from the exporter's.
        merged: dict[int, PolicyRecord] = {
            p.effective_version: p for p in t_policies}
        target_explicit = bool(t_policies)
        for p in policies:
            if p.effective_version in merged:
                continue  # never override a fact the target recorded
            if p.effective_version > t or not target_explicit:
                merged[p.effective_version] = p
        additions = [merged[ev] for ev in sorted(merged)
                     if not any(p.effective_version == ev for p in t_policies)]
        plans.append(SubjectPlan(
            name=name,
            new_versions=new_versions,
            new_policies=tuple(additions),
            already_present=overlap,
        ))
    return plans
