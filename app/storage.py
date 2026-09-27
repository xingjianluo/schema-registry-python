"""Thread-safe local storage for subjects, versions and policy history."""
from __future__ import annotations

import threading
from datetime import UTC, datetime

from .config import DEFAULT_COMPATIBILITY
from .models import CompatibilityLevel, PolicyRecord, RegisteredSchema


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


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

    # -- migration primitives ----------------------------------------------

    def snapshot(self, subjects: list[str] | None = None,
                 ) -> dict[str, tuple[list[RegisteredSchema],
                                      list[PolicyRecord]]]:
        """Point-in-time copy of versions + policy history.

        With *subjects* None, covers every known subject (including ones
        that only have a policy history).  One lock hold => consistent cut.
        """
        with self._lock:
            names = (sorted(set(self._subjects) | set(self._policies))
                     if subjects is None else subjects)
            return {
                name: (list(self._subjects.get(name, [])),
                       list(self._policies.get(name, [])))
                for name in names
            }

    def apply_import(self,
                     plans: list[tuple[str,
                                       list[RegisteredSchema],
                                       list[PolicyRecord]]]) -> None:
        """Append planned versions/policy records in one critical section.

        Only ever appends, so readers blocked on the lock see either the
        full pre-import or the full post-import state.
        """
        with self._lock:
            for name, new_versions, new_policies in plans:
                if new_versions:
                    self._subjects.setdefault(name, []).extend(new_versions)
                if new_policies:
                    self._policies.setdefault(name, []).extend(new_policies)
