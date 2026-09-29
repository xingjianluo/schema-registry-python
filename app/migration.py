"""Offline export/import of registry data between local instances.

A *bundle* is a plain JSON-compatible dict capturing, per subject:

* every registered version (number, schema, schema_id, registered_at),
* the subject's *base policy* (the level in force before any explicit
  policy change, i.e. the exporter's default when the subject was created),
* the full policy history (level, effective_version, changed_at),
* per-version derived facts used for integrity checks: the policy level
  in force at registration time and the compatibility conclusion reached
  under that policy.

Because the base policy travels inside the bundle, import never depends on
the importing instance's own default compatibility setting: a subject
exported under default FORWARD reproduces FORWARD conclusions even on an
instance whose default is BACKWARD (or anything else).

The whole payload is protected by an integrity envelope (a SHA-256 digest
of the canonical payload).  Any tampering, truncation or re-signing by a
third party is detected before any state is touched.

Import is two-phase: first the envelope is verified, the bundle is
validated and a plan is computed against a snapshot of the target (no
mutation), then the plan is applied in one short critical section.  Any
failure in phase one leaves the target untouched (nothing to roll back);
phase two only performs in-memory appends that cannot fail, so readers
either see the full pre-import or post-import state, never an
intermediate one.

Failure categories, intentionally distinguishable for callers:

* BundleCorruptError      -- envelope digest missing/bad or the bytes do
                             not parse as JSON at all: the package itself
                             was damaged in transit or tampered with;
                             the right action is to re-export/re-transfer.
* BundleMalformedError    -- envelope is authentic but the document has
                             the wrong shape/types or an unknown format,
                             level, code or digest algorithm: it could not
                             have been produced by a healthy exporter;
                             needs human investigation.
* BundleIncompleteError   -- authentic and well-shaped, but required
                             content is missing (a version body, a policy
                             block, the base policy, the conclusion):
                             the export must be redone / data supplied.
* BundleInconsistentError -- complete and well-shaped, but contents
                             contradict each other or the recomputed
                             facts: human investigation.

All functions in this module are pure: they never touch a store.
"""
from __future__ import annotations

import enum
import hashlib
import hmac
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
DIGEST_ALGORITHM = "sha256"
INTEGRITY_KEY = "integrity"


class ConflictMode(str, enum.Enum):
    """How import treats subjects that already exist in the target."""

    REJECT = "reject"  # any existing subject aborts the whole import
    MERGE = "merge"    # keep target versions, append only missing ones


class MigrationError(ValueError):
    """Base class for migration failures. Carries a machine-readable code."""

    code = "MIGRATION_ERROR"


class BundleCorruptError(MigrationError):
    """The package cannot be trusted: bytes do not parse as JSON or the
    integrity digest is missing/does not match the payload.  The package
    was truncated, damaged in transit or tampered with; re-export it."""

    code = "BUNDLE_CORRUPT"


class BundleMalformedError(MigrationError):
    """The envelope is authentic but the bundle is not a well-formed
    export document (structure/type, unknown format, level, code,
    algorithm)."""

    code = "BUNDLE_MALFORMED"


class BundleIncompleteError(MigrationError):
    """The envelope is authentic and the shape is fine, but required
    content is missing: a version body, policy information, the base
    policy or a recorded conclusion."""

    code = "BUNDLE_INCOMPLETE"


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


def policy_level_at(history: list[PolicyRecord], version: int,
                    base: CompatibilityLevel = DEFAULT_COMPATIBILITY
                    ) -> CompatibilityLevel:
    """Level in force when *version* was registered (pure replay).

    *base* is the level that governed the subject before its first
    explicit policy record; it comes from the bundle, never from the
    importing instance's configuration.
    """
    chosen: CompatibilityLevel | None = None
    for record in history:
        if record.effective_version <= version:
            chosen = record.level
        else:
            break
    return chosen if chosen is not None else base


def _conclusion_for(versions: list[RegisteredSchema], index: int,
                    history: list[PolicyRecord],
                    base: CompatibilityLevel
                    ) -> tuple[CompatibilityLevel, CompatibilityReport]:
    """Recompute the conclusion of versions[index] from raw data."""
    level = policy_level_at(history, versions[index].version, base)
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


# -- integrity envelope ---------------------------------------------------------


def _canonical(payload: object) -> bytes:
    """Deterministic byte representation the digest is taken over."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _digest(payload: object,
            algorithm: str = DIGEST_ALGORITHM) -> str:
    return hashlib.new(algorithm, _canonical(payload)).hexdigest()


def seal_bundle(payload: dict) -> dict:
    """Attach the integrity envelope to an unsigned payload.

    The digest covers every byte of the export document except the
    envelope itself, so changing any field (registered_at, schema_id,
    conclusions, ...) invalidates it.
    """
    if INTEGRITY_KEY in payload:
        payload = {k: v for k, v in payload.items() if k != INTEGRITY_KEY}
    sealed = dict(payload)
    sealed[INTEGRITY_KEY] = {
        "algorithm": DIGEST_ALGORITHM,
        "digest": _digest(payload),
    }
    return sealed


def _payload_of(bundle: dict) -> dict:
    return {k: v for k, v in bundle.items() if k != INTEGRITY_KEY}


def verify_envelope(bundle: object) -> dict:
    """Check the integrity envelope; return the unsigned payload.

    Raises BundleCorruptError when the envelope is absent or the digest
    does not match (truncated/tampered bytes), and BundleMalformedError
    when an authentic-looking envelope uses an unsupported algorithm.
    """
    if not isinstance(bundle, dict):
        raise BundleCorruptError("bundle must be a JSON object")
    envelope = bundle.get(INTEGRITY_KEY)
    if not isinstance(envelope, dict):
        raise BundleCorruptError(
            "integrity envelope is missing; the package is truncated or "
            "was not produced by an export")
    algorithm = envelope.get("algorithm")
    if not isinstance(algorithm, str):
        raise BundleCorruptError(
            "integrity envelope has no algorithm field (damaged package)")
    if algorithm not in hashlib.algorithms_available:
        raise BundleMalformedError(
            f"unsupported integrity algorithm {algorithm!r}")
    recorded = envelope.get("digest")
    if not isinstance(recorded, str):
        raise BundleCorruptError(
            "integrity envelope has no digest field (damaged package)")
    payload = _payload_of(bundle)
    actual = _digest(payload, algorithm)
    if not hmac.compare_digest(recorded, actual):
        raise BundleCorruptError(
            "integrity digest mismatch: the package was modified, "
            "truncated or damaged in transit; re-export and re-transfer it")
    return payload


def parse_bundle(raw: str | bytes) -> dict:
    """Parse an on-the-wire bundle document and verify its envelope.

    Bytes that do not parse as JSON are reported as BUNDLE_CORRUPT
    (truncated/damaged transport), never as a structural defect.
    """
    try:
        bundle = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise BundleCorruptError(
            f"package is not valid JSON (truncated or damaged): {exc}") from exc
    return verify_envelope(bundle)


# -- export ------------------------------------------------------------------


def build_bundle(subjects: dict[str, tuple[list[RegisteredSchema],
                                           list[PolicyRecord]]],
                 exported_at: str,
                 default_level: CompatibilityLevel = DEFAULT_COMPATIBILITY
                 ) -> dict:
    """Serialize a store snapshot into a sealed bundle dict.  Pure.

    *default_level* is the exporter's default policy; it is recorded per
    subject as the base policy so an importer with a different default
    reproduces history exactly.
    """
    out_subjects = []
    for name in sorted(subjects):
        versions, policies = subjects[name]
        # When a policy change was recorded effective from v1, the
        # exporter default never governed this subject: the first
        # explicit record is its effective base.
        base = (policies[0].level
                if policies and policies[0].effective_version == 1
                else default_level)
        out_versions = []
        for i, record in enumerate(versions):
            level, report = _conclusion_for(versions, i, policies,
                                           base)
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
            "base_policy": base.value,
            "versions": out_versions,
            "policies": [
                {"level": p.level.value,
                 "effective_version": p.effective_version,
                 "changed_at": p.changed_at}
                for p in policies
            ],
        })
    payload = {
        "format": BUNDLE_FORMAT,
        "format_version": BUNDLE_FORMAT_VERSION,
        "exported_at": exported_at,
        "subjects": out_subjects,
    }
    return seal_bundle(payload)


# -- validation ----------------------------------------------------------------


def _malformed(msg: str) -> BundleMalformedError:
    return BundleMalformedError(msg)


def _incomplete(msg: str) -> BundleIncompleteError:
    return BundleIncompleteError(msg)


def _inconsistent(msg: str) -> BundleInconsistentError:
    return BundleInconsistentError(msg)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise _malformed(msg)


def _require_field(obj: dict, key: str, where: str) -> object:
    """Fetch a required field; its *absence* is an incomplete package,
    while an explicit null/wrong type is a malformed one."""
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
    # Explicitly absent conclusion: the export is missing recorded data.
    if raw is None:
        raise _incomplete(f"{where}: recorded conclusion is missing")
    _require(isinstance(raw, dict), f"{where}: conclusion must be an object")
    if "compatible" not in raw:
        raise _incomplete(
            f"{where}: conclusion.compatible is missing")
    _require(isinstance(raw.get("compatible"), bool),
             f"{where}: conclusion.compatible must be a boolean")
    if "incompatibilities" not in raw:
        raise _incomplete(
            f"{where}: conclusion.incompatibilities is missing")
    raw_issues = raw.get("incompatibilities")
    _require(isinstance(raw_issues, list),
             f"{where}: conclusion.incompatibilities must be a list")
    issues: list[Incompatibility] = []
    for j, item in enumerate(raw_issues):
        ipath = f"{where}.incompatibilities[{j}]"
        _require(isinstance(item, dict), f"{ipath}: must be an object")
        for key in ("code", "message", "path"):
            if key not in item:
                raise _incomplete(f"{ipath}: {key!r} is missing")
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
    """Verify, parse and check a bundle end-to-end; return normalized
    subject entries.

    The order matters:

    1. integrity envelope  -> BundleCorruptError (never even looks at
       contents of an untrusted package),
    2. shape/types/format  -> BundleMalformedError (re-export cannot
       plausibly help; human investigation),
    3. required content    -> BundleIncompleteError (redo the export /
       supply the data),
    4. self-contradiction  -> BundleInconsistentError (human
       investigation).

    Each returned entry: {"name", "base" (CompatibilityLevel),
    "versions": [RegisteredSchema], "policies": [PolicyRecord]}.
    Pure.
    """
    payload = verify_envelope(bundle)  # corrupt packages stop here
    _require(payload.get("format") == BUNDLE_FORMAT,
             f"format must be {BUNDLE_FORMAT!r}")
    _require(payload.get("format_version") == BUNDLE_FORMAT_VERSION,
             f"unsupported format_version {payload.get('format_version')!r}")
    exported_at = _require_field(payload, "exported_at", "bundle")
    _require(isinstance(exported_at, str),
             "bundle.exported_at must be a string")
    raw_subjects = _require_field(payload, "subjects", "bundle")
    _require(isinstance(raw_subjects, list), "subjects must be a list")

    entries: list[dict] = []
    seen_names: set[str] = set()
    for s, raw_subject in enumerate(raw_subjects):
        where = f"subjects[{s}]"
        _require(isinstance(raw_subject, dict), f"{where}: must be an object")
        name = _require_field(raw_subject, "name", where)
        _require(isinstance(name, str) and name,
                 f"{where}: name must be a non-empty string")
        _require(name not in seen_names,
                 f"{where}: duplicate subject {name!r}")
        seen_names.add(name)

        base = _parse_level(
            _require_field(raw_subject, "base_policy", where),
            f"{where}.base_policy")

        raw_policies = _require_field(raw_subject, "policies", where)
        _require(isinstance(raw_policies, list),
                 f"{where}: policies must be a list")
        policies: list[PolicyRecord] = []
        prev_ev = 0
        for p, raw_policy in enumerate(raw_policies):
            pwhere = f"{where}.policies[{p}]"
            _require(isinstance(raw_policy, dict),
                     f"{pwhere}: must be an object")
            level = _parse_level(
                _require_field(raw_policy, "level", pwhere), pwhere)
            ev = _require_field(raw_policy, "effective_version", pwhere)
            _require(isinstance(ev, int) and not isinstance(ev, bool) and ev >= 1,
                     f"{pwhere}: effective_version must be an integer >= 1")
            changed_at = _require_field(raw_policy, "changed_at", pwhere)
            _require(isinstance(changed_at, str),
                     f"{pwhere}: changed_at must be a string")
            if ev < prev_ev:
                raise _inconsistent(
                    f"{pwhere}: effective_version {ev} goes backwards "
                    f"(previous {prev_ev})")
            prev_ev = ev
            policies.append(PolicyRecord(level, ev, changed_at))

        # A change effective from v1 means the base policy never actually
        # governed anything: the recorded base must equal that level.
        if policies and policies[0].effective_version == 1 \
                and base != policies[0].level:
            raise _inconsistent(
                f"{where}: base_policy {base.value} contradicts the "
                f"first policy record {policies[0].level.value} "
                f"effective from version 1")

        raw_versions = _require_field(raw_subject, "versions", where)
        _require(isinstance(raw_versions, list),
                 f"{where}: versions must be a list")
        if not raw_versions and not raw_policies:
            # A subject name with neither versions nor policy content can
            # never come from a real store snapshot: content is missing.
            raise _incomplete(
                f"{where}: subject carries no versions and no policy "
                f"information")
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
            number = _require_field(raw_version, "version", vwhere)
            _require(isinstance(number, int) and not isinstance(number, bool),
                     f"{vwhere}: version must be an integer")
            if number != v + 1:
                raise _inconsistent(
                    f"{vwhere}: versions must be dense 1..N, got {number} "
                    f"at position {v + 1}")
            schema = _require_field(raw_version, "schema", vwhere)
            _require(isinstance(schema, dict), f"{vwhere}: schema must be an object")
            schema_err = validate_schema(schema)
            if schema_err is not None:
                raise _inconsistent(f"{vwhere}: schema invalid: {schema_err}")
            schema_id = _require_field(raw_version, "schema_id", vwhere)
            _require(isinstance(schema_id, str),
                     f"{vwhere}: schema_id must be a string")
            if schema_id != canonical_schema_id(schema):
                raise _inconsistent(
                    f"{vwhere}: schema_id {schema_id!r} does not match "
                    f"schema content")
            registered_at = _require_field(raw_version, "registered_at",
                                           vwhere)
            _require(isinstance(registered_at, str),
                     f"{vwhere}: registered_at must be a string")
            recorded_level = _parse_level(
                _require_field(raw_version, "policy", vwhere), vwhere)
            recorded = _parse_conclusion(raw_version.get("conclusion"),
                                         vwhere)
            versions.append(RegisteredSchema(
                subject=name, version=number, schema=schema,
                schema_id=schema_id, registered_at=registered_at))
            # cross-check derived facts against recomputation under the
            # *bundle's* base policy, not the importer's default
            level, report = _conclusion_for(versions, v, policies, base)
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

        entries.append({"name": name, "base": base,
                        "exported_at": exported_at,
                        "versions": versions, "policies": policies})
    return entries


# -- import planning -----------------------------------------------------------


def _same_content(a: RegisteredSchema, b: RegisteredSchema) -> bool:
    return a.schema_id == b.schema_id and a.schema == b.schema


def plan_import(entries: list[dict],
                target: dict[str, tuple[list[RegisteredSchema],
                                        list[PolicyRecord]]],
                mode: ConflictMode,
                target_default: CompatibilityLevel = DEFAULT_COMPATIBILITY
                ) -> list[SubjectPlan]:
    """Compute the per-subject plan against a target snapshot.

    Raises ImportConflictError on any collision.  Pure: *target* is only
    read.  All subjects are planned before anything is applied, so a
    conflict anywhere aborts the whole import.

    *target_default* is the importing instance's own default policy; it
    is used only to decide whether the bundle's base policy needs an
    explicit seed record.  Imported conclusions never depend on it.
    """
    plans: list[SubjectPlan] = []
    for entry in entries:
        name = entry["name"]
        versions: list[RegisteredSchema] = entry["versions"]
        policies: list[PolicyRecord] = entry["policies"]
        base: CompatibilityLevel = entry["base"]
        existing = target.get(name)
        t_versions = list(existing[0]) if existing else []
        t_policies = list(existing[1]) if existing else []

        if mode is ConflictMode.REJECT and (t_versions or t_policies):
            raise ImportConflictError(
                f"subject {name!r} already exists in target "
                f"({len(t_versions)} versions); mode=reject")

        if not t_versions and not t_policies:
            # Brand-new subject: reproduce the exporter's base policy
            # explicitly when it is not already stated by a history
            # record effective from v1, so the subject's history is
            # self-contained regardless of the importer's own default.
            seeds: list[PolicyRecord] = []
            if not (policies and policies[0].effective_version == 1):
                changed_at = (policies[0].changed_at if policies
                              else (versions[0].registered_at
                                    if versions else entry["exported_at"]))
                seeds.append(PolicyRecord(base, 1, changed_at))
            plans.append(SubjectPlan(
                name=name,
                new_versions=tuple(versions),
                new_policies=tuple(seeds + list(policies)),
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
        # policy_at(t + 1) matches the bundle even then.  Replay uses the
        # bundle's own base policy, never the importer's default.
        wanted = policy_level_at(policies, t + 1, base)
        if policy_level_at(t_policies + additions, t + 1,
                           target_default) != wanted:
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
