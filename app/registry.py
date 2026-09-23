"""注册中心领域服务。

职责：
- 主体 Schema 注册与连续版本号分配（主体锁串行化）；
- 按“当前生效策略”做兼容性校验，拒绝时返回可区分原因；
- 预检（只读，不改变任何状态）；
- 策略调整与历史复现（复现默认使用该版本登记时的策略快照）；
- 版本化的预检结论缓存：策略或新版本登记后旧缓存自动失效。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .audit import AuditLogger
from .checker import check_compatibility, fingerprint, validate_schema
from .models import (
    CompatibilityLevel,
    CompatibilityReport,
    InvalidPolicyError,
    InvalidSchemaError,
    PolicyRecord,
    RegistrationResult,
    SchemaIncompatibleError,
    SchemaVersion,
    SubjectNotFoundError,
    VersionNotFoundError,
)
from .store import LocalStore, SubjectState

GLOBAL_SUBJECT = "*"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_level(value: str | CompatibilityLevel) -> CompatibilityLevel:
    if isinstance(value, CompatibilityLevel):
        return value
    try:
        return CompatibilityLevel(str(value).upper())
    except ValueError as exc:
        raise InvalidPolicyError(
            f"未知兼容性策略: {value!r}",
            {"allowed": [level.value for level in CompatibilityLevel]},
        ) from exc


@dataclass(frozen=True)
class _CacheKey:
    candidate_fp: str
    baseline_version: int
    policy_level: str
    policy_revision: int


class SchemaRegistry:
    def __init__(
        self,
        store: LocalStore | None = None,
        audit: AuditLogger | None = None,
        default_level: str | CompatibilityLevel = CompatibilityLevel.BACKWARD,
    ) -> None:
        self.store = store or LocalStore()
        self.audit = audit or AuditLogger()
        initial = parse_level(default_level)
        # 全局默认策略存于特殊主体 "*" 的 policy_history 中
        state = self.store.get_or_create_subject(GLOBAL_SUBJECT)
        if state.policy is None:
            self.store.append_policy(
                GLOBAL_SUBJECT,
                {"level": initial.value, "revision": 1, "updated_at": _utc_now()},
            )
        self._cache: dict[str, dict[_CacheKey, CompatibilityReport]] = {}
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------ 内部工具
    @staticmethod
    def _to_version(subject: str, raw: dict[str, Any]) -> SchemaVersion:
        return SchemaVersion(
            subject=subject,
            version=raw["version"],
            schema=raw["schema"],
            fingerprint=raw["fingerprint"],
            created_at=raw["created_at"],
        )

    def _effective_policy(self, state: SubjectState) -> dict[str, Any]:
        """当前生效策略：主体覆盖优先，否则使用全局默认。"""
        return state.policy or self.store.get_subject(GLOBAL_SUBJECT).policy  # type: ignore[union-attr]

    def _invalidate_cache(self, subject: str) -> None:
        with self._cache_lock:
            self._cache.pop(subject, None)

    def _invalidate_all_caches(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def _cached_check(
        self,
        subject: str,
        baseline: dict[str, Any],
        candidate: dict[str, Any],
        level: CompatibilityLevel,
        baseline_version: int,
        policy_revision: int,
    ) -> CompatibilityReport:
        key = _CacheKey(
            candidate_fp=fingerprint(candidate),
            baseline_version=baseline_version,
            policy_level=level.value,
            policy_revision=policy_revision,
        )
        with self._cache_lock:
            bucket = self._cache.setdefault(subject, {})
            cached = bucket.get(key)
        if cached is not None:
            self.audit.log(
                "compatibility_cache_hit",
                subject=subject,
                candidate_fingerprint=key.candidate_fp,
                baseline_version=baseline_version,
                policy_level=level.value,
                policy_revision=policy_revision,
            )
            return cached
        report = check_compatibility(baseline, candidate, level)
        with self._cache_lock:
            self._cache.setdefault(subject, {})[key] = report
        self.audit.log(
            "compatibility_cache_miss",
            subject=subject,
            candidate_fingerprint=key.candidate_fp,
            baseline_version=baseline_version,
            policy_level=level.value,
            policy_revision=policy_revision,
            compatible=report.compatible,
            incompatibilities=[item.to_dict() for item in report.incompatibilities],
        )
        return report

    # ------------------------------------------------------------ 注册
    def register(self, subject: str, schema: dict[str, Any]) -> RegistrationResult:
        errors = validate_schema(schema)
        if errors:
            self.audit.log(
                "register_rejected",
                subject=subject,
                reason="INVALID_SCHEMA",
                input_schema=schema,
                incompatibilities=[item.to_dict() for item in errors],
            )
            raise InvalidSchemaError("schema 结构非法", {"issues": [item.to_dict() for item in errors]})

        fp = fingerprint(schema)
        with self.store.subject_lock(subject):
            state = self.store.get_or_create_subject(subject)
            # 幂等：相同指纹返回既有版本
            for existing in state.versions:
                if existing["fingerprint"] == fp:
                    self.audit.log(
                        "register_duplicate",
                        subject=subject,
                        input_schema=schema,
                        fingerprint=fp,
                        version=existing["version"],
                        conclusion="already_registered",
                    )
                    policy = self._effective_policy(state)
                    return RegistrationResult(
                        subject=subject,
                        version=existing["version"],
                        fingerprint=fp,
                        already_registered=True,
                        report=self._identity_report(state, existing, policy),
                    )

            policy = self._effective_policy(state)
            level = parse_level(policy["level"])
            if state.versions:
                baseline_record = state.versions[-1]
                report = self._cached_check(
                    subject,
                    baseline_record["schema"],
                    schema,
                    level,
                    baseline_version=baseline_record["version"],
                    policy_revision=policy["revision"],
                )
                if not report.compatible:
                    self.audit.log(
                        "register_rejected",
                        subject=subject,
                        reason="SCHEMA_INCOMPATIBLE",
                        input_schema=schema,
                        fingerprint=fp,
                        baseline_version=baseline_record["version"],
                        policy_level=level.value,
                        policy_revision=policy["revision"],
                        incompatibilities=[item.to_dict() for item in report.incompatibilities],
                        conclusion="rejected",
                    )
                    raise SchemaIncompatibleError(subject, report)
            else:
                report = CompatibilityReport(
                    compatible=True,
                    level=level,
                    baseline_fingerprint="-",
                    candidate_fingerprint=fp,
                    incompatibilities=[],
                )

            next_version = len(state.versions) + 1
            record = {
                "subject": subject,
                "version": next_version,
                "schema": schema,
                "fingerprint": fp,
                "created_at": _utc_now(),
                "policy_snapshot": {"level": policy["level"], "revision": policy["revision"]},
            }
            self.store.append_version(subject, record)
            # 新版本成为最新基线：旧基线的缓存结论不再代表“最新版本”，整体失效
            self._invalidate_cache(subject)
            self.audit.log(
                "register_accepted",
                subject=subject,
                input_schema=schema,
                fingerprint=fp,
                version=next_version,
                policy_level=policy["level"],
                policy_revision=policy["revision"],
                basis="compatible_with_latest" if next_version > 1 else "first_version",
                conclusion="accepted",
            )
            return RegistrationResult(
                subject=subject,
                version=next_version,
                fingerprint=fp,
                already_registered=False,
                report=report,
            )

    def _identity_report(self, state: SubjectState, existing: dict[str, Any], policy: dict[str, Any]) -> CompatibilityReport:
        return check_compatibility(existing["schema"], existing["schema"], parse_level(policy["level"]))

    # ------------------------------------------------------------ 预检
    def precheck(self, subject: str, schema: dict[str, Any]) -> CompatibilityReport:
        """只读预检：主体不存在或尚无版本时，以空基线视为可注册。"""
        errors = validate_schema(schema)
        fp = fingerprint(schema)
        if errors:
            self.audit.log(
                "precheck",
                subject=subject,
                input_schema=schema,
                mutated_state=False,
                incompatibilities=[item.to_dict() for item in errors],
                conclusion="invalid_schema",
            )
            return CompatibilityReport(
                compatible=False,
                level=CompatibilityLevel.NONE,
                baseline_fingerprint="-",
                candidate_fingerprint=fp,
                incompatibilities=errors,
            )

        with self.store.subject_lock(subject):
            state = self.store.get_subject(subject)
            if state is None or not state.versions:
                report = CompatibilityReport(
                    compatible=True,
                    level=CompatibilityLevel.NONE,
                    baseline_fingerprint="-",
                    candidate_fingerprint=fp,
                    incompatibilities=[],
                )
                self.audit.log(
                    "precheck",
                    subject=subject,
                    input_schema=schema,
                    mutated_state=False,
                    baseline_version=None,
                    conclusion="compatible_empty_subject",
                )
                return report
            policy = self._effective_policy(state)
            level = parse_level(policy["level"])
            baseline_record = state.versions[-1]
            report = self._cached_check(
                subject,
                baseline_record["schema"],
                schema,
                level,
                baseline_version=baseline_record["version"],
                policy_revision=policy["revision"],
            )
            self.audit.log(
                "precheck",
                subject=subject,
                input_schema=schema,
                mutated_state=False,
                fingerprint=fp,
                baseline_version=baseline_record["version"],
                policy_level=level.value,
                policy_revision=policy["revision"],
                incompatibilities=[item.to_dict() for item in report.incompatibilities],
                conclusion="compatible" if report.compatible else "incompatible",
            )
            return report

    # ------------------------------------------------------------ 策略
    def get_policy(self, subject: str | None = None) -> PolicyRecord:
        if subject is None:
            global_state = self.store.get_subject(GLOBAL_SUBJECT)
            assert global_state is not None and global_state.policy is not None
            raw = global_state.policy
        else:
            state = self.store.get_subject(subject)
            if state is None:
                raise SubjectNotFoundError(f"主体不存在: {subject!r}")
            raw = self._effective_policy(state)
        return PolicyRecord(parse_level(raw["level"]), raw["revision"], raw["updated_at"])

    def set_global_policy(self, level: str | CompatibilityLevel) -> PolicyRecord:
        record = self._set_policy(GLOBAL_SUBJECT, level)
        # 全局策略影响所有未单独覆盖的主体，旧缓存结论必须全部失效
        self._invalidate_all_caches()
        return record

    def set_policy(self, subject: str, level: str | CompatibilityLevel) -> PolicyRecord:
        return self._set_policy(subject, level)

    def _set_policy(self, subject: str, level: str | CompatibilityLevel) -> PolicyRecord:
        parsed = parse_level(level)
        with self.store.subject_lock(subject):
            state = self.store.get_or_create_subject(subject)
            revision = (state.policy["revision"] + 1) if state.policy else 1
            record = {"level": parsed.value, "revision": revision, "updated_at": _utc_now()}
            self.store.append_policy(subject, record)
            # 策略变化：缓存结论可能基于旧策略，整体失效
            self._invalidate_cache(subject)
            self.audit.log(
                "policy_changed",
                subject=subject,
                new_level=parsed.value,
                revision=revision,
                previous_level=state.policy["level"] if state.policy else None,
                conclusion="applied_to_future_registrations_only",
            )
            return PolicyRecord(parsed, revision, record["updated_at"])

    def list_policy_history(self, subject: str) -> list[PolicyRecord]:
        state = self.store.get_subject(subject)
        if state is None:
            raise SubjectNotFoundError(f"主体不存在: {subject!r}")
        return [
            PolicyRecord(parse_level(item["level"]), item["revision"], item["updated_at"])
            for item in state.policy_history
        ]

    # ------------------------------------------------------------ 版本读取
    def list_versions(self, subject: str) -> list[SchemaVersion]:
        state = self.store.get_subject(subject)
        if state is None:
            raise SubjectNotFoundError(f"主体不存在: {subject!r}")
        return [self._to_version(subject, raw) for raw in state.versions]

    def get_version(self, subject: str, version: int) -> SchemaVersion:
        state = self.store.get_subject(subject)
        if state is None:
            raise SubjectNotFoundError(f"主体不存在: {subject!r}")
        for raw in state.versions:
            if raw["version"] == version:
                return self._to_version(subject, raw)
        raise VersionNotFoundError(f"主体 {subject!r} 不存在版本 {version}")

    def latest(self, subject: str) -> SchemaVersion:
        state = self.store.get_subject(subject)
        if state is None or not state.versions:
            raise SubjectNotFoundError(f"主体 {subject!r} 尚无已登记版本")
        return self._to_version(subject, state.versions[-1])

    # ------------------------------------------------------------ 历史复现
    def reproduce(
        self,
        subject: str,
        version: int,
        level: str | CompatibilityLevel | None = None,
    ) -> CompatibilityReport:
        """复现某版本登记时的兼容性结论。

        - ``level`` 为 None：使用该版本记录中固化的 ``policy_snapshot``（按当时策略复现）；
        - 显式传入 level：以指定策略与“该版本的上一版本”重新判定。
        """
        state = self.store.get_subject(subject)
        if state is None:
            raise SubjectNotFoundError(f"主体不存在: {subject!r}")
        target = next((raw for raw in state.versions if raw["version"] == version), None)
        if target is None:
            raise VersionNotFoundError(f"主体 {subject!r} 不存在版本 {version}")

        if level is None:
            snapshot = target.get("policy_snapshot")
            if snapshot is None:  # 极早期数据兜底
                raise InvalidPolicyError(f"版本 {version} 缺少策略快照，无法按当时策略复现")
            chosen = parse_level(snapshot["level"])
            revision = snapshot["revision"]
        else:
            chosen = parse_level(level)
            policy = self._effective_policy(state)
            revision = policy["revision"]

        if version == 1:
            report = CompatibilityReport(
                compatible=True,
                level=chosen,
                baseline_fingerprint="-",
                candidate_fingerprint=target["fingerprint"],
                incompatibilities=[],
            )
        else:
            previous = next(raw for raw in state.versions if raw["version"] == version - 1)
            # 历史复现不走“最新版本”缓存，直接确定性计算
            report = check_compatibility(previous["schema"], target["schema"], chosen)
        self.audit.log(
            "reproduce",
            subject=subject,
            version=version,
            requested_level=level.value if isinstance(level, CompatibilityLevel) else level,
            policy_level=chosen.value,
            policy_revision=revision,
            compatible=report.compatible,
            incompatibilities=[item.to_dict() for item in report.incompatibilities],
            conclusion="reproduced_historical_verdict",
        )
        return report
