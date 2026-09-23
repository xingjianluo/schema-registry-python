"""FastAPI 路由：注册、预检、版本、策略、历史复现。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, Request
from pydantic import BaseModel, Field

from .models import RegistryError
from .registry import GLOBAL_SUBJECT, SchemaRegistry

router = APIRouter()


def get_registry(request: Request) -> SchemaRegistry:
    return request.app.state.registry  # type: ignore[no-any-return]


class RegisterRequest(BaseModel):
    schema_: dict[str, Any] = Field(alias="schema")

    model_config = {"populate_by_name": True}


class PolicyRequest(BaseModel):
    compatibility: str


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/subjects")
def list_subjects(request: Request) -> list[str]:
    registry = get_registry(request)
    return [name for name in registry.store.list_subjects() if name != GLOBAL_SUBJECT]


@router.post("/subjects/{subject}/versions")
def register(subject: str, payload: RegisterRequest, request: Request) -> dict[str, Any]:
    registry = get_registry(request)
    result = registry.register(subject, payload.schema_)
    return {
        "subject": subject,
        "version": result.version,
        "id": result.fingerprint,
        "already_registered": result.already_registered,
        "compatibility_report": result.report.to_dict(),
    }


@router.post("/compatibility/subjects/{subject}/versions")
def precheck(subject: str, payload: RegisterRequest, request: Request) -> dict[str, Any]:
    registry = get_registry(request)
    report = registry.precheck(subject, payload.schema_)
    return {"subject": subject, "report": report.to_dict()}


@router.get("/subjects/{subject}/versions")
def list_versions(subject: str, request: Request) -> list[dict[str, Any]]:
    registry = get_registry(request)
    return [
        {
            "subject": item.subject,
            "version": item.version,
            "id": item.fingerprint,
            "created_at": item.created_at,
        }
        for item in registry.list_versions(subject)
    ]


@router.get("/subjects/{subject}/versions/{version}")
def get_version(subject: str, version: int, request: Request) -> dict[str, Any]:
    registry = get_registry(request)
    item = registry.get_version(subject, version)
    return {
        "subject": item.subject,
        "version": item.version,
        "schema": item.schema,
        "id": item.fingerprint,
        "created_at": item.created_at,
    }


@router.get("/subjects/{subject}/policy")
def get_policy(subject: str, request: Request) -> dict[str, Any]:
    registry = get_registry(request)
    record = registry.get_policy(subject)
    return {"subject": subject, "compatibility": record.level.value, "revision": record.revision}


@router.put("/subjects/{subject}/policy")
def set_policy(subject: str, payload: PolicyRequest, request: Request) -> dict[str, Any]:
    registry = get_registry(request)
    record = registry.set_policy(subject, payload.compatibility)
    return {"subject": subject, "compatibility": record.level.value, "revision": record.revision}


@router.get("/config")
def get_global_policy(request: Request) -> dict[str, Any]:
    registry = get_registry(request)
    record = registry.get_policy(None)
    return {"compatibility": record.level.value, "revision": record.revision}


@router.put("/config")
def set_global_policy(payload: PolicyRequest, request: Request) -> dict[str, Any]:
    registry = get_registry(request)
    record = registry.set_global_policy(payload.compatibility)
    return {"compatibility": record.level.value, "revision": record.revision}


@router.get("/subjects/{subject}/versions/{version}/verdict")
def reproduce_verdict(subject: str, version: int, request: Request, compatibility: str | None = None) -> dict[str, Any]:
    registry = get_registry(request)
    report = registry.reproduce(subject, version, compatibility)
    return {"subject": subject, "version": version, "report": report.to_dict()}


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RegistryError)
    async def handle_registry_error(_request: Request, exc: RegistryError) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())
