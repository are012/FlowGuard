"""FlowGuard FastAPI application factory."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from flowguard.api import router
from flowguard.observability import configure_logging, log_event
from flowguard.rate_limit import (
    InMemoryRateLimiter,
    OperationalMiddleware,
    limiter_from_environment,
)
from flowguard.services.analysis import AnalysisOrchestrator
from flowguard.services.data import DataService
from flowguard.services.errors import ServiceError
from flowguard.services.recommendations import RecommendationService
from flowguard.services.reports import ReportQueryService
from flowguard.services.tools import CoreToolService
from flowguard.storage import FlowGuardRepository, StorageError

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
logger = logging.getLogger("flowguard.api")


def create_app(
    repository: FlowGuardRepository | None = None,
    *,
    rate_limiter: InMemoryRateLimiter | None = None,
) -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="FlowGuard API",
        version="0.1.0",
        description="프리랜서를 위한 결정론적 13주 유동성 관리 API",
    )
    auto_create_schema = os.getenv("FLOWGUARD_AUTO_CREATE_SCHEMA", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    repository = repository or FlowGuardRepository(create_schema=auto_create_schema)
    tools = CoreToolService(repository)
    app.state.repository = repository
    app.state.data_service = DataService(repository)
    app.state.analysis_service = AnalysisOrchestrator(
        repository,
        tools=tools,
    )
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
        OperationalMiddleware,
        limiter=rate_limiter if rate_limiter is not None else limiter_from_environment(),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID", "X-User-ID"],
        expose_headers=[
            "RateLimit-Limit",
            "RateLimit-Remaining",
            "Retry-After",
            "X-Request-ID",
        ],
    )
    app.include_router(router)
    _register_error_handlers(app)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, error: ServiceError) -> JSONResponse:
        log_event(
            logger,
            logging.WARNING,
            "api_error",
            request_id=_request_id(request),
            error_code=error.code,
            status_code=error.http_status,
            retryable=error.retryable,
        )
        return JSONResponse(status_code=error.http_status, content=error.to_dict())

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "type": item["type"],
                "location": [str(part) for part in item["loc"]],
                "message": item["msg"],
            }
            for item in error.errors()
        ]
        log_event(
            logger,
            logging.WARNING,
            "api_error",
            request_id=_request_id(request),
            error_code="INVALID_REQUEST",
            status_code=422,
            retryable=False,
        )
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
    async def http_error_handler(request: Request, error: StarletteHTTPException) -> JSONResponse:
        log_event(
            logger,
            logging.WARNING,
            "api_error",
            request_id=_request_id(request),
            error_code="HTTP_ERROR",
            status_code=error.status_code,
            retryable=False,
        )
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
    async def storage_error_handler(request: Request, error: StorageError) -> JSONResponse:
        log_event(
            logger,
            logging.ERROR,
            "api_error",
            request_id=_request_id(request),
            error_code="STORAGE_ERROR",
            status_code=500,
            retryable=False,
            exception_type=type(error).__name__,
        )
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
    async def unhandled_error_handler(request: Request, error: Exception) -> JSONResponse:
        log_event(
            logger,
            logging.ERROR,
            "api_error",
            request_id=_request_id(request),
            error_code="INTERNAL_ERROR",
            status_code=500,
            retryable=False,
            exception_type=type(error).__name__,
        )
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": "요청을 처리하지 못했습니다.",
                "details": {},
                "retryable": False,
            },
        )


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


app = create_app()


def error_schema() -> dict[str, Any]:
    """Small public helper used by deployment smoke checks."""

    return {
        "code": "ERROR_CODE",
        "message": "사용자에게 안전한 오류 설명",
        "details": {},
        "retryable": False,
    }
