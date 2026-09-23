"""HTTP API for the schema registry."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .models import CompatibilityLevel
from .service import (
    IncompatibleSchemaError,
    RegistryService,
    SchemaValidationError,
)


def _report_body(report) -> dict:
    return {
        "is_compatible": report.compatible,
        "compatibility": report.level.value,
        "incompatibilities": [
            {"code": i.code.value, "message": i.message, "path": i.path,
             "detail": i.detail}
            for i in report.incompatibilities
        ],
    }


def create_app(service: RegistryService | None = None) -> FastAPI:
    service = service if service is not None else RegistryService()
    app = FastAPI(title="schema-registry-python")
    app.state.service = service

    @app.exception_handler(SchemaValidationError)
    async def _invalid(request: Request, exc: SchemaValidationError):
        return JSONResponse(
            status_code=422,
            content={"error": "SCHEMA_INVALID", "message": str(exc)},
        )

    @app.exception_handler(IncompatibleSchemaError)
    async def _incompatible(request: Request, exc: IncompatibleSchemaError):
        return JSONResponse(
            status_code=409,
            content={"error": "INCOMPATIBLE_SCHEMA",
                     **_report_body(exc.report)},
        )

    @app.exception_handler(KeyError)
    async def _not_found(request: Request, exc: KeyError):
        return JSONResponse(status_code=404,
                            content={"error": "NOT_FOUND", "message": str(exc)})

    @app.get("/")
    def read_root():
        return {"service": "schema-registry-python", "status": "ok"}

    @app.post("/subjects/{subject}/versions")
    def register(subject: str, payload: dict):
        schema = payload.get("schema")
        if not isinstance(schema, dict):
            raise SchemaValidationError("body must contain a 'schema' object")
        record = service.register(subject, schema)
        return {"subject": record.subject, "version": record.version,
                "schema_id": record.schema_id, "registered_at": record.registered_at}

    @app.post("/subjects/{subject}/compatibility")
    def precheck(subject: str, payload: dict):
        schema = payload.get("schema")
        if not isinstance(schema, dict):
            raise SchemaValidationError("body must contain a 'schema' object")
        level = payload.get("compatibility")
        parsed = CompatibilityLevel(level) if level else None
        report = service.precheck(subject, schema, parsed)
        return _report_body(report)

    @app.get("/subjects/{subject}/versions")
    def list_versions(subject: str):
        return [{"version": r.version, "schema_id": r.schema_id,
                 "schema": r.schema, "registered_at": r.registered_at}
                for r in service.list_versions(subject)]

    @app.get("/subjects/{subject}/versions/{version}")
    def get_version(subject: str, version: int):
        record = service.store.get_version(subject, version)
        if record is None:
            raise KeyError(f"{subject} v{version} not found")
        return {"subject": record.subject, "version": record.version,
                "schema_id": record.schema_id, "schema": record.schema,
                "registered_at": record.registered_at}

    @app.get("/subjects/{subject}/compatibility/recheck/{version}")
    def recheck(subject: str, version: int):
        report = service.recheck_history(subject, version)
        return {"subject": subject, "version": version, **_report_body(report)}

    @app.get("/subjects/{subject}/compatibility/policy")
    def get_policy(subject: str):
        record = service.get_policy(subject)
        return {"compatibility": record.level.value,
                "effective_version": record.effective_version,
                "changed_at": record.changed_at}

    @app.put("/subjects/{subject}/compatibility/policy")
    def set_policy(subject: str, payload: dict):
        level = payload.get("compatibility")
        if level not in [l.value for l in CompatibilityLevel]:
            raise SchemaValidationError(
                f"unknown compatibility level {level!r}; "
                f"expected one of {[l.value for l in CompatibilityLevel]}")
        record = service.set_policy(subject, CompatibilityLevel(level))
        return {"compatibility": record.level.value,
                "effective_version": record.effective_version,
                "changed_at": record.changed_at}

    return app
