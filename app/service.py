"""Application service: orchestrates storage, checker and cache."""
from __future__ import annotations

import hashlib
import json
import logging
import threading

from .cache import ConclusionCache
from .checker import check_compatibility, validate_schema
from .models import (
    CompatibilityLevel,
    CompatibilityReport,
    PolicyRecord,
    RegisteredSchema,
)
from .storage import SchemaStore, SubjectConflictError

logger = logging.getLogger("schema_registry")


class SchemaValidationError(ValueError):
    """Raised when a submitted schema document is malformed."""


class IncompatibleSchemaError(ValueError):
    """Raised when registration fails a compatibility policy."""

    def __init__(self, report: CompatibilityReport) -> None:
        self.report = report
        codes = ", ".join(i.code.value for i in report.incompatibilities)
        super().__init__(f"schema rejected under {report.level.value}: {codes}")


def canonical_schema_id(schema: dict) -> str:
    """Stable id: identical schema documents share an id."""
    blob = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class RegistryService:
    def __init__(self, store: SchemaStore | None = None,
                 cache: ConclusionCache | None = None) -> None:
        self.store = store if store is not None else SchemaStore()
        self.cache = cache if cache is not None else ConclusionCache()
        # Serializes check-then-register so concurrent registrations of the
        # same subject always observe dense, unique version numbers.
        self._register_lock = threading.RLock()

    # -- registration ------------------------------------------------------

    def register(self, subject: str, schema: dict) -> RegisteredSchema:
        err = validate_schema(schema)
        if err is not None:
            logger.warning("register rejected (invalid) subject=%s error=%s",
                           subject, err)
            raise SchemaValidationError(str(err))

        candidate_id = canonical_schema_id(schema)
        with self._register_lock:
            latest = self.store.latest_version(subject)
            policy = self.store.get_policy(subject)
            report = check_compatibility(
                latest.schema if latest else None, schema, policy.level)
            if not report.compatible:
                logger.warning(
                    "register rejected subject=%s level=%s reasons=%s",
                    subject, policy.level.value,
                    [i.code.value for i in report.incompatibilities])
                raise IncompatibleSchemaError(report)
            record = self.store.register(subject, schema, candidate_id)
            self.cache.invalidate_subject(subject)
            self.cache.note_version(subject, record.version)
            logger.info(
                "register accepted subject=%s version=%d id=%s level=%s",
                subject, record.version, candidate_id, policy.level.value)
            return record

    # -- precheck (read-only) ---------------------------------------------

    def precheck(self, subject: str, schema: dict,
                 level: CompatibilityLevel | None = None) -> CompatibilityReport:
        """Check compatibility without registering.

        Never mutates registered state.  Results may be memoized in the
        conclusion cache, which is invalidated as versions advance.
        """
        err = validate_schema(schema)
        if err is not None:
            raise SchemaValidationError(str(err))

        policy = self.store.get_policy(subject).level if level is None else level
        latest = self.store.latest_version(subject)
        latest_no = latest.version if latest else 0
        candidate_id = canonical_schema_id(schema)

        cached = self.cache.get(subject, candidate_id, policy)
        if cached is not None:
            logger.info("precheck cache hit subject=%s compatible=%s",
                        subject, cached.compatible)
            return cached

        report = check_compatibility(
            latest.schema if latest else None, schema, policy)
        self.cache.put(subject, candidate_id, policy, report, latest_no)
        logger.info(
            "precheck computed subject=%s against=v%s level=%s compatible=%s reasons=%s",
            subject, latest_no, policy.value, report.compatible,
            [i.code.value for i in report.incompatibilities])
        return report

    # -- policy ------------------------------------------------------------

    def set_policy(self, subject: str, level: CompatibilityLevel) -> PolicyRecord:
        record = self.store.set_policy(subject, level)
        # Old conclusions were derived under the old policy: drop them.
        self.cache.invalidate_subject(subject)
        latest = self.store.latest_version(subject)
        if latest is not None:
            self.cache.note_version(subject, latest.version)
        logger.info("policy changed subject=%s level=%s effective_from=v%s",
                    subject, level.value, record.effective_version)
        return record

    def get_policy(self, subject: str) -> PolicyRecord:
        return self.store.get_policy(subject)

    def list_versions(self, subject: str) -> list[RegisteredSchema]:
        return self.store.list_versions(subject)

    def recheck_history(self, subject: str, version: int) -> CompatibilityReport:
        """Reproduce the compatibility conclusion of a historical version.

        The check always runs between version N-1 and N under the policy
        that was effective when N was registered.
        """
        current = self.store.get_version(subject, version)
        if current is None:
            raise KeyError(f"{subject} has no version {version}")
        policy = self.store.policy_at(subject, version)
        previous = self.store.get_version(subject, version - 1)
        report = check_compatibility(
            previous.schema if previous else None, current.schema, policy.level)
        logger.info(
            "recheck subject=%s version=%d level_at_then=%s compatible=%s",
            subject, version, policy.level.value, report.compatible)
        return report

    # -- migration ---------------------------------------------------------

    def export_subjects(self, subjects: list[str] | None = None) -> bytes:
        """Offline-export one subject, a list, or all subjects (None).

        The store snapshot and every historical conclusion is produced
        under one service-level lock, so the package always reflects a
        single consistent moment (no mixed-before/after state).
        """
        from . import migration
        from .config import DEFAULT_COMPATIBILITY

        with self._register_lock:
            snapshot = self.store.snapshot(subjects)
            exports = []
            for item in snapshot:
                conclusions = []
                for idx, rec in enumerate(item.versions):
                    previous = item.versions[idx - 1] if idx > 0 else None
                    level = self._policy_at_in(item.policies,
                                               item.default_level, rec.version)
                    report = check_compatibility(
                        previous.schema if previous else None,
                        rec.schema, level)
                    conclusions.append(migration.VersionConclusion(
                        version=rec.version,
                        level=report.level,
                        compatible=report.compatible,
                        incompatibility_codes=tuple(
                            i.code.value for i in report.incompatibilities),
                    ))
                exports.append((item, tuple(conclusions)))
            package = migration.build_package(exports, DEFAULT_COMPATIBILITY)
            data = migration.encode_package(package)
        logger.info("export subjects=%s bytes=%d",
                    [i.subject for i in snapshot], len(data))
        return data

    @staticmethod
    def _policy_at_in(policies, default_level, version):
        """Policy in force at *version* within an exported snapshot."""
        chosen = default_level
        for r in policies:
            if r.effective_version <= version:
                chosen = r.level
            else:
                break
        return chosen

    def import_package(
        self,
        data: bytes,
        mode: object = None,
    ) -> list:
        """Validate and atomically load an offline migration package.

        Package parsing, checksum verification and historical-conclusion
        re-verification all happen before the store is touched.  The commit
        runs under the same lock serializing registrations, so other
        callers observe either the full pre-import or full post-import
        state, never an intermediate one.
        """
        from . import migration

        if mode is None:
            mode = migration.ConflictMode.REJECT
        if isinstance(mode, str):
            try:
                mode = migration.ConflictMode(mode)
            except ValueError as exc:
                raise migration.MigrationError(
                    f"unknown conflict mode {mode!r}; "
                    f"expected REJECT or MERGE") from exc

        package = migration.decode_package(data)  # categorized errors
        items = migration.to_imports(package)

        with self._register_lock:
            try:
                results = self.store.import_subjects(
                    items, merge=mode is migration.ConflictMode.MERGE)
            except SubjectConflictError as exc:
                raise migration.ImportConflictError(
                    exc.subject, exc.version,
                    content_differs=exc.content_differs) from exc
            for item in items:
                self.cache.invalidate_subject(item.subject)
                latest = self.store.latest_version(item.subject)
                if latest is not None:
                    self.cache.note_version(item.subject, latest.version)

        logger.info(
            "import mode=%s results=%s",
            mode.value,
            [(r.subject, r.created, r.added_versions) for r in results])
        return results
