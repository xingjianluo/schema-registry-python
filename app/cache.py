"""Cache for compatibility conclusions with version-aware invalidation.

A cached entry remembers the latest registered version of its subject at
the moment it was computed.  It is only served while the subject still has
that same latest version, so a conclusion cached before a newer
registration (or a policy change that triggers explicit invalidation) can
never contradict the current registered state.
"""
from __future__ import annotations

import threading

from .models import CompatibilityLevel, CompatibilityReport


class ConclusionCache:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        # key -> (report, latest_version_when_cached)
        self._entries: dict[str, tuple[CompatibilityReport, int]] = {}
        # subject -> latest_version the cache has observed
        self._subject_latest: dict[str, int] = {}

    def make_key(self, subject: str, candidate_id: str,
                 level: CompatibilityLevel) -> str:
        return f"{subject}\x1f{level.value}\x1f{candidate_id}"

    def get(self, subject: str, candidate_id: str,
            level: CompatibilityLevel) -> CompatibilityReport | None:
        with self._lock:
            entry = self._entries.get(
                self.make_key(subject, candidate_id, level))
            if entry is None:
                return None
            report, cached_latest = entry
            # Stale if the subject gained versions after caching.
            current_latest = self._subject_latest.get(subject)
            if current_latest is not None and current_latest != cached_latest:
                self._entries.pop(
                    self.make_key(subject, candidate_id, level), None)
                return None
            return report

    def put(self, subject: str, candidate_id: str,
            level: CompatibilityLevel, report: CompatibilityReport,
            latest_version: int) -> None:
        with self._lock:
            self._entries[self.make_key(subject, candidate_id, level)] = (
                report, latest_version)
            prev = self._subject_latest.get(subject)
            if prev is None or latest_version > prev:
                self._subject_latest[subject] = latest_version


    def note_version(self, subject: str, latest_version: int) -> None:
        """Record the subject's current latest registered version."""
        with self._lock:
            prev = self._subject_latest.get(subject)
            if prev is None or latest_version > prev:
                self._subject_latest[subject] = latest_version

    def invalidate_subject(self, subject: str) -> None:
        """Drop every cached conclusion for *subject* (e.g. policy change)."""
        with self._lock:
            keys = [k for k in self._entries if k.startswith(subject + "\x1f")]
            for k in keys:
                self._entries.pop(k, None)

    def valid_latest(self, subject: str, latest_version: int) -> bool:
        with self._lock:
            return self._subject_latest.get(subject) == latest_version
