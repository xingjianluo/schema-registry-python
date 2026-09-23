"""测试夹具与日志配置：让输入、判定依据、结论出现在测试日志中。"""
from __future__ import annotations

import logging
import sys

import pytest

from app.audit import AuditLogger
from app.registry import SchemaRegistry
from app.store import LocalStore


@pytest.fixture()
def registry(tmp_path):
    db = tmp_path / "registry.json"
    audit = AuditLogger(logger_name=f"schema_registry.test.{tmp_path.name}")
    reg = SchemaRegistry(LocalStore(str(db)), audit=audit)
    reg.audit  # noqa: B018
    return reg


def pytest_configure(config: object) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(name)s | %(message)s"))
    root = logging.getLogger("schema_registry")
    root.setLevel(logging.INFO)
    if not any(getattr(h, "_pytest_handler", False) for h in root.handlers):
        handler._pytest_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)
