"""本地 JSON 存储：线程安全的主体状态与原子落盘。"""
from __future__ import annotations

import dataclasses
import json
import os
import threading
from typing import Any


@dataclasses.dataclass
class SubjectState:
    """单个主体的内存状态。"""

    versions: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    policy_history: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    @property
    def policy(self) -> dict[str, Any] | None:
        return self.policy_history[-1] if self.policy_history else None


class LocalStore:
    """JSON 文件 + 线程安全内存态。

    - 主体维度使用独立 RLock：注册同一主体串行化，不同主体可并行。
    - 全局 ``_write_lock`` 保证快照落盘原子性（tmp + os.replace）。
    """

    def __init__(self, path: str = "data/registry.json") -> None:
        self.path = path
        self._data: dict[str, Any] = {"subjects": {}}
        self._locks_guard = threading.Lock()
        self._subject_locks: dict[str, threading.RLock] = {}
        self._write_lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------ 并发原语
    def subject_lock(self, subject: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._subject_locks.get(subject)
            if lock is None:
                lock = threading.RLock()
                self._subject_locks[subject] = lock
            return lock

    # ------------------------------------------------------------ 状态访问
    def get_or_create_subject(self, subject: str) -> SubjectState:
        raw = self._data["subjects"].setdefault(subject, {"versions": [], "policy_history": []})
        return self._hydrate(raw)

    def get_subject(self, subject: str) -> SubjectState | None:
        raw = self._data["subjects"].get(subject)
        return self._hydrate(raw) if raw is not None else None

    def list_subjects(self) -> list[str]:
        return sorted(self._data["subjects"].keys())

    @staticmethod
    def _hydrate(raw: dict[str, Any]) -> SubjectState:
        return SubjectState(
            versions=list(raw.get("versions", [])),
            policy_history=list(raw.get("policy_history", [])),
        )

    # ------------------------------------------------------------ 变更
    def append_version(self, subject: str, record: dict[str, Any]) -> None:
        """调用方必须持有该主体的 subject_lock。"""
        raw = self._data["subjects"].setdefault(subject, {"versions": [], "policy_history": []})
        raw["versions"].append(record)
        self.save()

    def append_policy(self, subject: str, record: dict[str, Any]) -> None:
        raw = self._data["subjects"].setdefault(subject, {"versions": [], "policy_history": []})
        raw["policy_history"].append(record)
        self.save()

    # ------------------------------------------------------------ 持久化
    def save(self) -> None:
        with self._write_lock:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.path)

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict) or not isinstance(payload.get("subjects"), dict):
            raise ValueError(f"存储文件格式非法: {self.path}")
        self._data = payload
