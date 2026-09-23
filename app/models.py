"""Domain types for the schema registry.

Only type definitions live here; no behavior.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class CompatibilityLevel(str, enum.Enum):
    """Compatibility policies understood by the registry."""

    NONE = "NONE"
    BACKWARD = "BACKWARD"
    FORWARD = "FORWARD"
    FULL = "FULL"


class IncompatibilityCode(str, enum.Enum):
    """Machine-distinguishable categories of incompatibility."""

    SCHEMA_INVALID = "SCHEMA_INVALID"
    FIELD_REMOVED = "FIELD_REMOVED"
    FIELD_ADDED_REQUIRED = "FIELD_ADDED_REQUIRED"
    FIELD_TYPE_CHANGED = "FIELD_TYPE_CHANGED"
    ENUM_SYMBOL_REMOVED = "ENUM_SYMBOL_REMOVED"
    ENUM_SYMBOL_ADDED = "ENUM_SYMBOL_ADDED"


@dataclass(frozen=True, slots=True)
class Incompatibility:
    code: IncompatibilityCode
    message: str
    path: str = "/"
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompatibilityReport:
    compatible: bool
    level: CompatibilityLevel
    incompatibilities: tuple[Incompatibility, ...] = ()


@dataclass(frozen=True, slots=True)
class RegisteredSchema:
    subject: str
    version: int
    schema: dict
    schema_id: str
    registered_at: str


@dataclass(frozen=True, slots=True)
class PolicyRecord:
    """A policy value together with the ordinal at which it became effective."""

    level: CompatibilityLevel
    effective_version: int
    changed_at: str
