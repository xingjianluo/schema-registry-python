"""领域模型与错误分类（仅类型定义）。"""
from __future__ import annotations

import dataclasses
import enum
from typing import Any


class CompatibilityLevel(str, enum.Enum):
    """兼容性策略。"""

    NONE = "NONE"
    BACKWARD = "BACKWARD"
    FORWARD = "FORWARD"
    FULL = "FULL"


class IncompatibilityCode(str, enum.Enum):
    """不兼容原因的可区分分类。"""

    SCHEMA_INVALID = "SCHEMA_INVALID"
    FIELD_REMOVED = "FIELD_REMOVED"
    FIELD_TYPE_CHANGED = "FIELD_TYPE_CHANGED"
    REQUIRED_FIELD_ADDED = "REQUIRED_FIELD_ADDED"
    REQUIRED_FIELD_REMOVED = "REQUIRED_FIELD_REMOVED"
    ENUM_SYMBOL_REMOVED = "ENUM_SYMBOL_REMOVED"
    ENUM_SYMBOL_ADDED = "ENUM_SYMBOL_ADDED"


@dataclasses.dataclass(frozen=True)
class Incompatibility:
    code: IncompatibilityCode
    path: str
    message: str
    detail: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "path": self.path,
            "message": self.message,
            "detail": self.detail,
        }


@dataclasses.dataclass(frozen=True)
class CompatibilityReport:
    compatible: bool
    level: CompatibilityLevel
    baseline_fingerprint: str
    candidate_fingerprint: str
    incompatibilities: list[Incompatibility]

    def to_dict(self) -> dict[str, Any]:
        return {
            "compatible": self.compatible,
            "compatibility_level": self.level.value,
            "baseline_fingerprint": self.baseline_fingerprint,
            "candidate_fingerprint": self.candidate_fingerprint,
            "incompatibilities": [item.to_dict() for item in self.incompatibilities],
        }


@dataclasses.dataclass(frozen=True)
class SchemaVersion:
    subject: str
    version: int
    schema: dict[str, Any]
    fingerprint: str
    created_at: str


@dataclasses.dataclass(frozen=True)
class PolicyRecord:
    level: CompatibilityLevel
    revision: int
    updated_at: str


@dataclasses.dataclass(frozen=True)
class RegistrationResult:
    subject: str
    version: int
    fingerprint: str
    already_registered: bool
    report: CompatibilityReport


class RegistryError(Exception):
    """所有注册中心错误的基类，携带稳定的错误码与 HTTP 状态。"""

    code = "REGISTRY_ERROR"
    http_status = 400

    def __init__(self, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error_code": self.code, "message": self.message, "detail": self.detail}


class SubjectNotFoundError(RegistryError):
    code = "SUBJECT_NOT_FOUND"
    http_status = 404


class VersionNotFoundError(RegistryError):
    code = "VERSION_NOT_FOUND"
    http_status = 404


class InvalidSchemaError(RegistryError):
    code = "INVALID_SCHEMA"
    http_status = 422


class InvalidPolicyError(RegistryError):
    code = "INVALID_POLICY"
    http_status = 422


class SchemaIncompatibleError(RegistryError):
    code = "SCHEMA_INCOMPATIBLE"
    http_status = 409

    def __init__(self, subject: str, report: CompatibilityReport):
        super().__init__(
            f"schema 与主体 {subject!r} 的最新版本不兼容",
            {"report": report.to_dict()},
        )
        self.subject = subject
        self.report = report
