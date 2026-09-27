"""Thread-safe local storage for subjects, versions and policy history."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import DEFAULT_COMPATIBILITY
from .models import CompatibilityLevel, PolicyRecord, RegisteredSchema


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _same_schema(a: dict, b: dict) -> bool:
    """Structural equality independent of dict key order / whitespace."""
    import json

    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


@dataclass(frozen=True, slots=True)
class SubjectImport:
    """One subject's full registration data to load into a store.

    ``policies`` carry *source* effective-version coordinates; the store
    rewrites them when merging into a target that already has versions.
    ``default_level`` is the source instance default policy, used to
    synthesize the implicit v1 policy record on a fresh target.
    """

    subject: str
    versions: list[RegisteredSchema]
    policies: list[PolicyRecord]
    default_level: CompatibilityLevel


@dataclass(frozen=True, slots=True)
class SubjectImportResult:
    subject: str
    created: bool
    added_versions: tuple[int, ...] = ()
    skipped_versions: tuple[int, ...] = ()


class SubjectConflictError(Exception):
    """Target refuses an incoming subject.

    *content_differs* is True when an overlapping version has different
    content (a real content conflict); False for REJECT mode refusing an
    already-present, identical subject (pure name collision).
    """

    def __init__(self, subject: str, version: int,
                 content_differs: bool = True) -> None:
        self.subject = subject
        self.version = version
        self.content_differs = content_differs
        if content_differs:
            msg = (f"subject {subject!r} version {version} exists with "
                   f"different content")
        else:
            msg = (f"subject {subject!r} already exists "
                   f"(REJECT mode refuses name collisions even when identical)")
        super().__init__(msg)


class SchemaStore:
    """In-memory store. All mutating operations are serialized by one lock."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subjects: dict[str, list[RegisteredSchema]] = {}
        # policy history per subject, kept sorted by effective_version.
        self._policies: dict[str, list[PolicyRecord]] = {}

    # -- versions ----------------------------------------------------------

    def register(self, subject: str, schema: dict, schema_id: str) -> RegisteredSchema:
        """Append a new version. Versions are dense: 1, 2, 3, ..."""
        with self._lock:
            versions = self._subjects.setdefault(subject, [])
            record = RegisteredSchema(
                subject=subject,
                version=len(versions) + 1,
                schema=schema,
                schema_id=schema_id,
                registered_at=_utcnow(),
            )
            versions.append(record)
            return record

    def get_version(self, subject: str, version: int) -> RegisteredSchema | None:
        with self._lock:
            versions = self._subjects.get(subject, [])
            if 1 <= version <= len(versions):
                return versions[version - 1]
            return None

    def list_versions(self, subject: str) -> list[RegisteredSchema]:
        with self._lock:
            return list(self._subjects.get(subject, []))

    def latest_version(self, subject: str) -> RegisteredSchema | None:
        with self._lock:
            versions = self._subjects.get(subject)
            return versions[-1] if versions else None

    # -- policy history ----------------------------------------------------

    def set_policy(self, subject: str, level: CompatibilityLevel) -> PolicyRecord:
        """Change the policy. It applies to registrations made afterwards."""
        with self._lock:
            history = self._policies.setdefault(subject, [])
            next_version = len(self._subjects.get(subject, [])) + 1
            record = PolicyRecord(
                level=level,
                effective_version=next_version,
                changed_at=_utcnow(),
            )
            history.append(record)
            return record

    def get_policy(self, subject: str) -> PolicyRecord:
        """Current effective policy (applied to the *next* registration)."""
        with self._lock:
            history = self._policies.get(subject)
            if history:
                return history[-1]
            return PolicyRecord(
                level=DEFAULT_COMPATIBILITY,
                effective_version=1,
                changed_at=_utcnow(),
            )

    def policy_at(self, subject: str, version: int) -> PolicyRecord:
        """Policy in force when *version* was registered.

        Picks the most recent change whose effective_version <= version;
        when no explicit change exists yet, returns the default policy.
        """
        with self._lock:
            history = self._policies.get(subject, [])
            chosen: PolicyRecord | None = None
            for record in history:
                if record.effective_version <= version:
                    chosen = record
                else:
                    break
            if chosen is not None:
                return chosen
            return PolicyRecord(
                level=DEFAULT_COMPATIBILITY,
                effective_version=1,
                changed_at=_utcnow(),
            )

    def list_policies(self, subject: str) -> list[PolicyRecord]:
        with self._lock:
            return list(self._policies.get(subject, []))

    # -- migration ---------------------------------------------------------

    def list_subjects(self) -> list[str]:
        with self._lock:
            return sorted(self._subjects.keys())

    def has_subject(self, subject: str) -> bool:
        with self._lock:
            return subject in self._subjects

    def snapshot(self, subjects: list[str] | None) -> list[SubjectImport]:
        """Return consistent copies of the named subjects (or all).

        Raises KeyError if a requested subject does not exist.  All copies
        are taken under the same lock acquisition, so an export can never
        observe versions of different subjects at different moments.
        """
        with self._lock:
            names = sorted(self._subjects.keys()) if subjects is None \
                else list(subjects)
            missing = [s for s in names if s not in self._subjects]
            if missing:
                raise KeyError(f"subject(s) not found: {missing}")
            return [
                SubjectImport(
                    subject=name,
                    versions=list(self._subjects[name]),
                    policies=list(self._policies.get(name, [])),
                    default_level=DEFAULT_COMPATIBILITY,
                )
                for name in names
            ]

    def import_subjects(
        self, items: list[SubjectImport], merge: bool
    ) -> list[SubjectImportResult]:
        """Atomically create-or-reject / create-or-merge all *items*.

        Must hold the service-level register lock when called, so that
        concurrent registrations can never interleave with an import.

        All conflicts are detected before any dictionary is mutated, so a
        failure leaves the store exactly as it was.  Subjects not named in
        *items* are never touched.
        """
        with self._lock:
            # Phase 1: validate every item and plan the resulting state.
            plans: list[tuple[SubjectImport, bool, list[int], list[int],
                              list[PolicyRecord]]] = []
            for item in items:
                existing = self._subjects.get(item.subject)
                if existing is None:
                    policies = self._plan_create_policies(item)
                    plans.append((item, True,
                                  [r.version for r in item.versions], [],
                                  policies))
                    continue

                if not merge:
                    differs = any(
                        v <= len(existing)
                        and not _same_schema(existing[v - 1].schema,
                                             item.versions[v - 1].schema)
                        for v in range(1, len(item.versions) + 1))
                    raise SubjectConflictError(
                        item.subject, existing[0].version,
                        content_differs=differs)

                added, skipped, policies = self._plan_merge(item, existing)
                plans.append((item, False, added, skipped, policies))

            # Phase 2: all checks passed — commit in one sweep.
            results = []
            for item, created, added, skipped, policies in plans:
                if created:
                    self._subjects[item.subject] = list(item.versions)
                    self._policies[item.subject] = policies
                elif added:
                    t = len(self._subjects[item.subject])
                    self._subjects[item.subject].extend(
                        item.versions[t:t + len(added)])
                    self._policies[item.subject] = policies
                results.append(SubjectImportResult(
                    subject=item.subject, created=created,
                    added_versions=tuple(added),
                    skipped_versions=tuple(skipped)))
            return results

    # -- migration planning -----------------------------------------------

    def _governing_policy(
        self, item: SubjectImport, version: int
    ) -> tuple[CompatibilityLevel, str | None]:
        """Source policy level in force at *version*, and its change time."""
        chosen: PolicyRecord | None = None
        for record in item.policies:
            if record.effective_version <= version:
                chosen = record
            else:
                break
        if chosen is not None:
            return chosen.level, chosen.changed_at
        return item.default_level, None

    def _plan_create_policies(self, item: SubjectImport) -> list[PolicyRecord]:
        """Reproduce source policy history on a fresh subject.

        Keeps every source record (including a still-pending one) and
        synthesizes an explicit v1 record carrying the source default when
        the source history started only later.
        """
        history = list(item.policies)
        if not any(r.effective_version <= 1 for r in history):
            level, changed_at = self._governing_policy(item, 1)
            baseline = PolicyRecord(
                level=level, effective_version=1,
                changed_at=changed_at or _utcnow())
            history.insert(0, baseline)
        history.sort(key=lambda r: r.effective_version)
        return history

    def _plan_merge(
        self,
        item: SubjectImport,
        existing: list[RegisteredSchema],
    ) -> tuple[list[int], list[int], list[PolicyRecord]]:
        """Plan an append-only merge; raise on same-number/different-content."""
        t = len(existing)
        s = len(item.versions)

        skipped: list[int] = []
        for v in range(1, min(t, s) + 1):
            incoming = item.versions[v - 1]
            if not _same_schema(existing[v - 1].schema, incoming.schema):
                raise SubjectConflictError(item.subject, v)
            skipped.append(v)

        added = list(range(t + 1, s + 1))

        history = list(self._policies.get(item.subject, []))
        if added:
            # Keep source policy changes strictly inside the appended
            # range; the source's still-pending policy is dropped so the
            # target keeps its own intent for future registrations.
            carried = [r for r in item.policies if t < r.effective_version <= s]
            if not any(r.effective_version <= t + 1 for r in carried):
                level, changed_at = self._governing_policy(item, t + 1)
                stamp = changed_at or item.versions[t].registered_at
                carried.insert(0, PolicyRecord(
                    level=level, effective_version=t + 1, changed_at=stamp))
            history = sorted(history + carried,
                             key=lambda r: r.effective_version)
        return added, skipped, history
