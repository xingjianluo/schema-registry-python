"""Application service: orchestrates storage, checker and cache."""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

from .cache import ConclusionCache
from .checker import canonical_schema_id, check_compatibility, validate_schema
from .migration import (
    ConflictMode,
    ImportSummary,
    build_bundle,
    plan_import,
    validate_bundle,
)
from .models import (
    CompatibilityLevel,
    CompatibilityReport,
    PolicyRecord,
    RegisteredSchema,
)
from .storage import SchemaStore

logger = logging.getLogger("schema_registry")


class SchemaValidationError(ValueError):
    """Raised when a submitted schema document is malformed."""


class IncompatibleSchemaError(ValueError):
    """Raised when registration fails a compatibility policy."""

    def __init__(self, report: CompatibilityReport) -> None:
        self.report = report
        codes = ", ".join(i.code.value for i in report.incompatibilities)
        super().__init__(f"schema rejected under {report.level.value}: {codes}")


__all__ = [
    "IncompatibleSchemaError",
    "RegistryService",
    "SchemaValidationError",
    "canonical_schema_id",
]


class RegistryService:
    def __init__(self, store: SchemaStore | None = None,
                 cache: ConclusionCache | None = None,
                 default_level: CompatibilityLevel | None = None) -> None:
        if store is None:
            store = SchemaStore(default_level) if default_level is not None \
                else SchemaStore()
        self.store = store
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
        # Serialized with registrations and imports so a policy change can
        # never interleave with an import's check-then-apply and silently
        # rewrite the policy that governs a just-imported version.
        with self._register_lock:
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

    # -- migration -----------------------------------------------------------

    def export_subjects(self, subjects: list[str]) -> dict:
        """Snapshot *subjects* into a self-contained, verifiable bundle.

        The snapshot is taken under the store lock, so the bundle is a
        consistent point-in-time cut; concurrent registrations either
        land entirely before or entirely after it.
        """
        if not subjects:
            raise SchemaValidationError("no subjects given to export")
        snapshot = self.store.snapshot(list(subjects))
        missing = [n for n in subjects
                   if not snapshot[n][0] and not snapshot[n][1]]
        if missing:
            raise KeyError(f"unknown subjects: {', '.join(sorted(missing))}")
        bundle = build_bundle(
            snapshot, datetime.now(UTC).isoformat(),
            default_level=self.store.default_level)
        logger.info(
            "export subjects=%s versions=%s",
            sorted(subjects),
            {n: len(snapshot[n][0]) for n in subjects})
        return bundle

    def import_bundle(self, bundle: dict,
                      on_conflict: str = "reject") -> ImportSummary:
        """Import a bundle atomically.

        Phase 1 (validate + plan) runs without mutating anything; any
        malformed/inconsistent/conflicting input aborts before phase 2,
        so there is never a half-imported state to roll back.  Phase 2
        applies all appends in one store critical section.  The whole
        check-then-apply is serialized against registrations and policy
        changes via the register lock, while plain readers only ever
        observe the state before or after the short apply.
        """
        entries = validate_bundle(bundle)  # raises before any mutation
        try:
            mode = ConflictMode(on_conflict)
        except ValueError:
            raise SchemaValidationError(
                f"unknown on_conflict mode {on_conflict!r}; "
                f"expected 'reject' or 'merge'")
        with self._register_lock:
            snapshot = self.store.snapshot([e["name"] for e in entries])
            plans = plan_import(entries, snapshot, mode,
                                target_default=self.store.default_level)
            self.store.apply_import([
                (p.name, list(p.new_versions), list(p.new_policies))
                for p in plans
            ])
            for p in plans:
                # Imported versions change what 'latest' means here.
                self.cache.invalidate_subject(p.name)
                latest = self.store.latest_version(p.name)
                if latest is not None:
                    self.cache.note_version(p.name, latest.version)
        summary = ImportSummary(tuple(plans))
        logger.info(
            "import mode=%s subjects=%s imported_versions=%d already_present=%s",
            mode.value,
            {p.name: len(p.new_versions) for p in plans},
            summary.imported_versions,
            {p.name: p.already_present for p in plans if p.already_present})
        return summary
