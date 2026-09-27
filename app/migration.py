"""Offline export/import of registration data between local instances.

A migration package bundles, per subject: every registered version (with
its schema_id and original registration time), the full compatibility-
policy history, and the historical compatibility conclusion recorded for
each version.  Packages are self-describing JSON envelopes carrying
per-subject and overall checksums so that truncation/tampering is rejected
before any state changes.

Pure logic only: encoding/decoding/validation live here; locking and
store mutation stay in :mod:`app.service` and :mod:`app.storage`.
"""
from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .checker import check_compatibility, validate_schema
from .models import (
    CompatibilityLevel,
    PolicyRecord,
    RegisteredSchema,
)
from .storage import SubjectImport

PACKAGE_FORMAT = "schema-registry-migration"
PACKAGE_VERSION = 1
_LEVELS = {l.value: l for l in CompatibilityLevel}


class ConflictMode(str, enum.Enum):
    """How import treats a subject that already exists on the target."""

    REJECT = "REJECT"  # whole import fails on any name collision
    MERGE = "MERGE"  # keep target versions; append only missing ones


class MigrationError(Exception):
    """Base class for all migration failures (whole import is refused)."""

    category = "MIGRATION_ERROR"


class PackageCorruptedError(MigrationError):
    """Envelope is not parseable JSON / wrong shape / checksum mismatch."""

    category = "PACKAGE_CORRUPTED"


class PackageInconsistentError(MigrationError):
    """Internally contradictory: dense numbering, unknown levels, bad refs."""

    category = "PACKAGE_INCONSISTENT"


class PackageContentMissingError(MigrationError):
    """A version payload or other required data is absent/unusable."""

    category = "PACKAGE_CONTENT_MISSING"


class ImportConflictError(MigrationError):
    """Same subject+version exists with different content (REJECT/MERGE).

    Under REJECT an identical but already-present subject is also refused;
    *content_differs* distinguishes the two for callers and logs.
    """

    category = "IMPORT_CONFLICT"

    def __init__(self, subject: str, version: int,
                 content_differs: bool = True) -> None:
        self.subject = subject
        self.version = version
        self.content_differs = content_differs
        if content_differs:
            msg = (f"conflict: subject {subject!r} version {version} exists "
                   f"with different content")
        else:
            msg = (f"conflict: subject {subject!r} already exists with "
                   f"identical content (REJECT mode refuses any name "
                   f"collision; use MERGE to no-op)")
        super().__init__(msg)


@dataclass(frozen=True, slots=True)
class VersionConclusion:
    """Historical conclusion recorded for a version at export time."""

    version: int
    level: CompatibilityLevel
    compatible: bool
    incompatibility_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SubjectPayload:
    subject: str
    versions: list[RegisteredSchema]
    policies: list[PolicyRecord]
    default_level: CompatibilityLevel
    conclusions: tuple[VersionConclusion, ...] = ()
    checksum: str = ""


@dataclass(frozen=True, slots=True)
class MigrationPackage:
    exported_at: str
    default_level: CompatibilityLevel
    subjects: tuple[SubjectPayload, ...]
    checksum: str


def _canonical(payload: object) -> bytes:
    """Deterministic byte form used both for checksums and on the wire."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _digest(payload: object) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _version_dict(r: RegisteredSchema) -> dict:
    return {"version": r.version, "schema_id": r.schema_id,
            "schema": r.schema, "registered_at": r.registered_at}


def _policy_dict(r: PolicyRecord) -> dict:
    return {"level": r.level.value,
            "effective_version": r.effective_version,
            "changed_at": r.changed_at}


def _conclusion_dict(c: VersionConclusion) -> dict:
    return {"version": c.version, "level": c.level.value,
            "compatible": c.compatible,
            "incompatibility_codes": list(c.incompatibility_codes)}


def _subject_body(p: SubjectPayload) -> dict:
    return {
        "subject": p.subject,
        "default_level": p.default_level.value,
        "versions": [_version_dict(v) for v in p.versions],
        "policies": [_policy_dict(r) for r in p.policies],
        "conclusions": [_conclusion_dict(c) for c in p.conclusions],
    }


def encode_package(payload: MigrationPackage) -> bytes:
    """Serialize a validated package to canonical UTF-8 JSON bytes.

    Per-subject checksums cover the subject body; the root checksum covers
    the package metadata and every (already checksummed) subject body, so
    either local tampering or subject-level truncation is detectable.
    """
    subjects = []
    for p in payload.subjects:
        body = _subject_body(p)
        subjects.append({"checksum": _digest(body), "body": body})
    envelope = {
        "format": PACKAGE_FORMAT,
        "package_version": PACKAGE_VERSION,
        "exported_at": payload.exported_at,
        "default_level": payload.default_level.value,
        "subjects": subjects,
    }
    envelope["checksum"] = _digest({k: v for k, v in envelope.items()})
    return _canonical(envelope)


def _require(cond: bool, exc: MigrationError) -> None:
    if not cond:
        raise exc


def _corrupted(msg: str) -> PackageCorruptedError:
    return PackageCorruptedError(msg)


def _inconsistent(msg: str) -> PackageInconsistentError:
    return PackageInconsistentError(msg)


def _missing(msg: str) -> PackageContentMissingError:
    return PackageContentMissingError(msg)


def _level(value: object, where: str) -> CompatibilityLevel:
    if not isinstance(value, str) or value not in _LEVELS:
        raise _inconsistent(
            f"{where}: unknown compatibility level {value!r}")
    return _LEVELS[value]  # type: ignore[return-value]


def decode_package(data: bytes) -> MigrationPackage:
    """Parse, structurally validate and verify checksums of *data*.

    Failure categories never overlap:

    * PackageCorruptedError  - unparseable/truncated JSON, wrong envelope
      shape or checksum mismatch;
    * PackageContentMissingError - a version's schema document is absent;
    * PackageInconsistentError   - parseable data that contradicts itself
      (numbering gaps, unknown levels, id/history mismatches).
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise _corrupted("migration package is empty or not byte data")
    try:
        envelope = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _corrupted(f"package is not valid JSON: {exc}") from exc

    _require(isinstance(envelope, dict),
             _corrupted("envelope must be a JSON object"))
    _require(envelope.get("format") == PACKAGE_FORMAT,
             _corrupted(f"not a {PACKAGE_FORMAT!r} package: "
                        f"format={envelope.get('format')!r}"))
    _require(envelope.get("package_version") == PACKAGE_VERSION,
             _corrupted(f"unsupported package_version: "
                        f"{envelope.get('package_version')!r}"))

    subjects_raw = envelope.get("subjects")
    _require(isinstance(subjects_raw, list),
             _corrupted("envelope.subjects must be a list"))
    exported_at = envelope.get("exported_at")
    _require(isinstance(exported_at, str) and exported_at,
             _corrupted("envelope.exported_at must be a non-empty string"))
    default_level = _level(envelope.get("default_level"),
                           "envelope.default_level")

    # Root checksum covers everything except the checksum itself.
    claimed_root = envelope.get("checksum")
    root_input = {k: v for k, v in envelope.items() if k != "checksum"}
    _require(isinstance(claimed_root, str)
             and _digest(root_input) == claimed_root,
             _corrupted("root checksum mismatch: package truncated or tampered"))

    decoded = [_decode_subject(entry) for entry in subjects_raw]
    names = [p.subject for p in decoded]
    _require(len(names) == len(set(names)),
             _inconsistent(f"duplicate subjects inside one package: {names}"))

    return MigrationPackage(
        exported_at=exported_at,
        default_level=default_level,
        subjects=tuple(decoded),
        checksum=claimed_root,
    )


def _decode_subject(entry: object) -> SubjectPayload:
    prefix = "envelope.subjects[]"
    _require(isinstance(entry, dict),
             _corrupted(f"{prefix}: each entry must be an object"))
    claimed = entry.get("checksum")
    body = entry.get("body")
    _require(isinstance(claimed, str) and isinstance(body, dict),
             _corrupted(f"{prefix}: entry requires 'checksum' and 'body'"))
    _require(_digest(body) == claimed,
             _corrupted(f"{prefix}: subject checksum mismatch"))

    subject = body.get("subject")
    _require(isinstance(subject, str) and subject,
             _corrupted(f"{prefix}.body.subject must be a non-empty string"))
    where = f"subject {subject!r}"
    subject_default = _level(body.get("default_level"),
                             f"{where}.default_level")

    versions = _decode_versions(body.get("versions"), where, subject)
    policies = _decode_policies(body.get("policies"), where, len(versions))
    conclusions = _decode_conclusions(
        body.get("conclusions"), where, subject_default, versions, policies)

    return SubjectPayload(
        subject=subject,
        versions=versions,
        policies=policies,
        default_level=subject_default,
        conclusions=conclusions,
        checksum=claimed,
    )


def _canonical_schema_id(schema: dict) -> str:
    """Same derivation as service.canonical_schema_id (kept local to avoid
    a service->migration import cycle)."""
    blob = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _decode_versions(raw: object, where: str,
                     subject: str) -> list[RegisteredSchema]:
    _require(isinstance(raw, list) and raw,
             _missing(f"{where}: versions list is missing or empty"))
    versions: list[RegisteredSchema] = []
    for i, v in enumerate(raw):
        vwhere = f"{where}.versions[{i}]"
        _require(isinstance(v, dict),
                 _corrupted(f"{vwhere}: must be an object"))
        no = v.get("version")
        _require(isinstance(no, int) and not isinstance(no, bool) and no >= 1,
                 _inconsistent(f"{vwhere}.version must be a positive integer"))
        _require(no == i + 1,
                 _inconsistent(
                     f"{where}: versions must be dense 1..N starting at 1; "
                     f"expected v{i + 1}, got v{no}"))
        schema = v.get("schema")
        if not isinstance(schema, dict):
            raise _missing(f"{vwhere}: schema document is missing")
        err = validate_schema(schema)
        if err is not None:
            raise _inconsistent(f"{vwhere}: schema is not valid: {err}")
        schema_id = v.get("schema_id")
        _require(isinstance(schema_id, str) and schema_id,
                 _inconsistent(f"{vwhere}.schema_id must be a non-empty string"))
        _require(_canonical_schema_id(schema) == schema_id,
                 _inconsistent(
                     f"{vwhere}: schema_id does not match schema content"))
        registered_at = v.get("registered_at")
        _require(isinstance(registered_at, str) and registered_at,
                 _inconsistent(f"{vwhere}.registered_at must be a string"))
        versions.append(RegisteredSchema(
            subject=subject,
            version=no, schema=schema, schema_id=schema_id,
            registered_at=registered_at))
    return versions


def _decode_policies(raw: object, where: str,
                     n_versions: int) -> list[PolicyRecord]:
    if raw is None:
        raise _missing(f"{where}: policies history is missing")
    _require(isinstance(raw, list),
             _corrupted(f"{where}.policies must be a list"))
    records: list[PolicyRecord] = []
    prev = 0
    for i, p in enumerate(raw):
        pwhere = f"{where}.policies[{i}]"
        _require(isinstance(p, dict),
                 _corrupted(f"{pwhere}: must be an object"))
        eff = p.get("effective_version")
        _require(isinstance(eff, int) and not isinstance(eff, bool)
                 and eff >= 1,
                 _inconsistent(
                     f"{pwhere}.effective_version must be a positive integer"))
        _require(eff > prev,
                 _inconsistent(
                     f"{where}: policy effective_versions must be strictly "
                     f"increasing; {eff} follows {prev}"))
        _require(eff <= n_versions + 1,
                 _inconsistent(
                     f"{pwhere}: effective_version {eff} points beyond the "
                     f"last version (have {n_versions})"))
        level = _level(p.get("level"), f"{pwhere}.level")
        changed_at = p.get("changed_at")
        _require(isinstance(changed_at, str) and changed_at,
                 _inconsistent(f"{pwhere}.changed_at must be a string"))
        records.append(PolicyRecord(level=level, effective_version=eff,
                                    changed_at=changed_at))
        prev = eff
    return records


def _governing(policies: list[PolicyRecord], default: CompatibilityLevel,
               version: int) -> CompatibilityLevel:
    chosen = default
    for r in policies:
        if r.effective_version <= version:
            chosen = r.level
        else:
            break
    return chosen


def _decode_conclusions(raw: object, where: str,
                        default_level: CompatibilityLevel,
                        versions: list[RegisteredSchema],
                        policies: list[PolicyRecord]
                        ) -> tuple[VersionConclusion, ...]:
    if raw is None:
        raise _missing(f"{where}: conclusions list is missing")
    _require(isinstance(raw, list),
             _corrupted(f"{where}.conclusions must be a list"))
    by_version: dict[int, VersionConclusion] = {}
    for i, c in enumerate(raw):
        cwhere = f"{where}.conclusions[{i}]"
        _require(isinstance(c, dict),
                 _corrupted(f"{cwhere}: must be an object"))
        no = c.get("version")
        _require(isinstance(no, int) and not isinstance(no, bool) and no >= 1,
                 _inconsistent(f"{cwhere}.version must be a positive integer"))
        _require(1 <= no <= len(versions),
                 _inconsistent(
                     f"{cwhere}: references v{no}, subject only has "
                     f"{len(versions)} versions"))
        _require(no not in by_version,
                 _inconsistent(f"{cwhere}: duplicate conclusion for v{no}"))
        level = _level(c.get("level"), f"{cwhere}.level")
        compatible = c.get("compatible")
        _require(isinstance(compatible, bool),
                 _inconsistent(f"{cwhere}.compatible must be a boolean"))
        codes = c.get("incompatibility_codes")
        _require(isinstance(codes, list)
                 and all(isinstance(x, str) for x in codes),
                 _inconsistent(
                     f"{cwhere}.incompatibility_codes must be a list of strings"))
        by_version[no] = VersionConclusion(
            version=no, level=level, compatible=compatible,
            incompatibility_codes=tuple(codes))

    # Cross-check: exactly one conclusion per version, and it must agree
    # with (a) the policy in force at that version per the bundled history
    # and (b) a fresh re-evaluation between v(n-1) and v(n) under it.
    _require(set(by_version) == set(range(1, len(versions) + 1)),
             _inconsistent(
                 f"{where}: conclusions must cover every version exactly once"))
    result = []
    for idx, rec in enumerate(versions):
        no = idx + 1
        c = by_version[no]
        expected_level = _governing(policies, default_level, no)
        _require(c.level is expected_level,
                 _inconsistent(
                     f"{where} v{no}: conclusion level {c.level.value!r} "
                     f"disagrees with policy history {expected_level.value!r}"))
        previous = versions[idx - 1].schema if idx > 0 else None
        report = check_compatibility(previous, rec.schema, expected_level)
        _require(report.compatible == c.compatible,
                 _inconsistent(
                     f"{where} v{no}: recorded conclusion compatible="
                     f"{c.compatible} does not reproduce under "
                     f"{expected_level.value} (got {report.compatible})"))
        reproduced_codes = {i.code.value for i in report.incompatibilities}
        _require(reproduced_codes == set(c.incompatibility_codes),
                 _inconsistent(
                     f"{where} v{no}: recorded reason codes "
                     f"{sorted(c.incompatibility_codes)} do not reproduce "
                     f"(got {sorted(reproduced_codes)})"))
        result.append(c)
    return tuple(result)


def to_imports(package: MigrationPackage) -> list[SubjectImport]:
    """Project a decoded package onto store-level import DTOs."""
    return [
        SubjectImport(
            subject=p.subject,
            versions=list(p.versions),
            policies=list(p.policies),
            default_level=p.default_level,
        )
        for p in package.subjects
    ]


def build_package(
    exports: list[tuple[SubjectImport, tuple[VersionConclusion, ...]]],
    instance_default: CompatibilityLevel,
    exported_at: str | None = None,
) -> MigrationPackage:
    """Assemble a package from store snapshots plus recorded conclusions.

    Inputs come from a consistent store snapshot; conclusions are computed
    by the service under each version's then-effective policy.
    """
    return MigrationPackage(
        exported_at=exported_at or datetime.now(timezone.utc).isoformat(),
        default_level=instance_default,
        subjects=tuple(
            SubjectPayload(
                subject=item.subject,
                versions=list(item.versions),
                policies=list(item.policies),
                default_level=item.default_level,
                conclusions=conclusions,
            )
            for item, conclusions in exports),
        checksum="",
    )
