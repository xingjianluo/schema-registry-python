"""审计日志：关键操作的输入、判定依据与结论同时输出到 logging 和内存流。"""
from __future__ import annotations

import collections
import json
import logging
from collections import deque
from typing import Any


class AuditLogger:
    """结构化审计记录器。

    - 通过标准库 logging 输出单行 JSON（便于在测试日志中查看输入/依据/结论）。
    - 内存中保留最近 ``capacity`` 条，供测试断言。
    """

    def __init__(self, logger_name: str = "schema_registry.audit", capacity: int = 1000) -> None:
        self._logger = logging.getLogger(logger_name)
        self._entries: deque[dict[str, Any]] = collections.deque(maxlen=capacity)

    def log(self, event: str, **fields: Any) -> None:
        entry: dict[str, Any] = {"event": event, **fields}
        self._entries.append(entry)
        try:
            line = json.dumps(entry, ensure_ascii=False, default=str, sort_keys=True)
        except TypeError:  # pragma: no cover - 兜底
            line = repr(entry)
        self._logger.info(line)

    def entries(self) -> list[dict[str, Any]]:
        return list(self._entries)
