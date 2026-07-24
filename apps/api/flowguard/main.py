"""FlowGuard FastAPI application factory."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from flowguard.api import router
from flowguard.services.analysis import AnalysisOrchestrator
from flowguard.services.data import DataService
from flowguard.services.errors import ServiceError
from flowguard.services.recommendations import RecommendationService
from flowguard.services.reports import ReportQueryService
from flowguard.services.tools import CoreToolService
from flowguard.storage import FlowGuardRepository, StorageError


def create_app(repository: FlowGuardRepository | None = None) -> FastAPI:
    app = FastAPI(
        title="FlowGuard API",
        version="0.1.0",
        description="프리랜서를 위한 결정론적 13주 유동성 관리 API",
    )
    repository = repository or FlowGuardRepository()
    tools = CoreToolService(repository)
    app.state.repository = repository
    app.state.data_service = DataService(repository)
    app.state.analysis_service = AnalysisOrchestrator(repository, tools=tools)
    app.state.recommendation_service = RecommendationService(
        repository, tools, app.state.analysis_service
    )
    app.state.report_service = ReportQueryService(repository)

    origins = [
        item.strip()
        for item in os.getenv("FLOWGUARD_CORS_ORIGINS", "http://localhost:3000").split(",")
        if item.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "OPTIONS"],
        allow_headers=["Content-Type", "X-User-ID"],
    )
    app.include_router(router)
    _register_error_handlers(app)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def service_error_handler(_request: Request, error: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=error.http_status, content=error.to_dict())

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        _request: Request, error: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "type": item["type"],
                "location": [str(part) for part in item["loc"]],
                "message": item["msg"],
            }
            for item in error.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "code": "INVALID_REQUEST",
                "message": "요청 형식이 올바르지 않습니다.",
                "details": {"validation_errors": details},
                "retryable": False,
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request: Request, error: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={
                "code": "HTTP_ERROR",
                "message": str(error.detail),
                "details": {},
                "retryable": False,
            },
        )

    @app.exception_handler(StorageError)
    async def storage_error_handler(_request: Request, _error: StorageError) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "code": "STORAGE_ERROR",
                "message": "금융 데이터를 저장하거나 조회하지 못했습니다.",
                "details": {},
                "retryable": False,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_request: Request, _error: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": "요청을 처리하지 못했습니다.",
                "details": {},
                "retryable": False,
            },
        )


app = create_app()


def error_schema() -> dict[str, Any]:
    """Small public helper used by deployment smoke checks."""

    return {
        "code": "ERROR_CODE",
        "message": "사용자에게 안전한 오류 설명",
        "details": {},
        "retryable": False,
    }
