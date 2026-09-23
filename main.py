"""服务入口：组装存储、注册中心与 FastAPI 路由。"""
from __future__ import annotations

import os

from fastapi import FastAPI

from app.api import install_exception_handlers, router
from app.registry import SchemaRegistry
from app.store import LocalStore

DB_PATH = os.environ.get("SCHEMA_REGISTRY_DB", "data/registry.json")
DEFAULT_LEVEL = os.environ.get("SCHEMA_REGISTRY_DEFAULT_POLICY", "BACKWARD")

app = FastAPI(title="Local Schema Registry", version="0.1.0")
app.state.registry = SchemaRegistry(LocalStore(DB_PATH), default_level=DEFAULT_LEVEL)
app.include_router(router)
install_exception_handlers(app)


@app.get("/")
def read_root() -> dict[str, str]:
    return {"service": "schema-registry", "status": "ok"}
