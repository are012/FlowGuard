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
from flowguard.config import (
    AI_CONNECT_TIMEOUT_SECONDS,
    AI_DEFAULT_LOCALE,
    AI_MAX_RETRIES,
    AI_RESPONSE_TIMEOUT_SECONDS,
    AI_TOTAL_TIMEOUT_SECONDS,
)
from flowguard.observability import configure_logging, log_event
from flowguard.rate_limit import (
    InMemoryRateLimiter,
    OperationalMiddleware,
    limiter_from_environment,
)
from flowguard.services.analysis import AnalysisOrchestrator
from flowguard.services.data import ClassificationMode, DataService, LabelClassifier
from flowguard.services.errors import ServiceError
from flowguard.services.investigation_loop import (
    AIInvestigationClient,
    InvestigationAIClient,
)
from flowguard.services.investigator import InvestigationMode
from flowguard.services.label_group_validation import AILabelClassificationClient
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
    classification_client: LabelClassifier | None = None,
    investigation_client: InvestigationAIClient | None = None,
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
    classification_mode = _classification_mode()
    investigation_mode = _investigation_mode()
    if classification_mode != "off" and classification_client is None:
        classification_client = AILabelClassificationClient(
            base_url=os.getenv("FLOWGUARD_AI_SERVER_URL", "http://localhost:8001"),
            connect_timeout_seconds=float(
                os.getenv(
                    "FLOWGUARD_AI_CONNECT_TIMEOUT_SECONDS",
                    str(AI_CONNECT_TIMEOUT_SECONDS),
                )
            ),
            read_timeout_seconds=float(
                os.getenv(
                    "FLOWGUARD_AI_RESPONSE_TIMEOUT_SECONDS",
                    str(AI_RESPONSE_TIMEOUT_SECONDS),
                )
            ),
            total_timeout_seconds=float(
                os.getenv(
                    "FLOWGUARD_AI_TOTAL_TIMEOUT_SECONDS",
                    str(AI_TOTAL_TIMEOUT_SECONDS),
                )
            ),
            max_retries=int(os.getenv("FLOWGUARD_AI_MAX_RETRIES", str(AI_MAX_RETRIES))),
        )
    if investigation_mode != "off" and investigation_client is None:
        investigation_client = AIInvestigationClient(
            base_url=os.getenv("FLOWGUARD_AI_SERVER_URL", "http://localhost:8001"),
            connect_timeout_seconds=float(
                os.getenv(
                    "FLOWGUARD_AI_CONNECT_TIMEOUT_SECONDS",
                    str(AI_CONNECT_TIMEOUT_SECONDS),
                )
            ),
            read_timeout_seconds=float(
                os.getenv(
                    "FLOWGUARD_AI_RESPONSE_TIMEOUT_SECONDS",
                    str(AI_RESPONSE_TIMEOUT_SECONDS),
                )
            ),
            max_retries=int(os.getenv("FLOWGUARD_AI_MAX_RETRIES", str(AI_MAX_RETRIES))),
        )
    app.state.repository = repository
    app.state.data_service = DataService(
        repository,
        classification_mode=classification_mode,
        classification_client=classification_client,
        classification_locale=os.getenv("FLOWGUARD_AI_LOCALE", AI_DEFAULT_LOCALE),
        classification_model_name=os.getenv("FLOWGUARD_AI_MODEL_NAME") or None,
    )
    app.state.analysis_service = AnalysisOrchestrator(
        repository,
        tools=tools,
        investigation_mode=investigation_mode,
        investigation_client=investigation_client,
        investigation_locale=os.getenv("FLOWGUARD_AI_LOCALE", AI_DEFAULT_LOCALE),
        investigation_model_name=os.getenv("FLOWGUARD_AI_MODEL_NAME", "gpt-5.6-luna"),
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


def _classification_mode() -> ClassificationMode:
    configured = os.getenv("FLOWGUARD_AI_CLASSIFICATION", "off").strip().lower()
    if configured in {"shadow", "on"}:
        return configured
    if configured not in {"", "off"}:
        log_event(
            logger,
            logging.WARNING,
            "invalid_ai_classification_mode",
            configured_mode=configured,
        )
    return "off"


def _investigation_mode() -> InvestigationMode:
    configured = os.getenv("FLOWGUARD_AI_INVESTIGATION", "off").strip().lower()
    if configured in {"shadow", "on"}:
        return configured
    if configured not in {"", "off"}:
        log_event(
            logger,
            logging.WARNING,
            "invalid_ai_investigation_mode",
            configured_mode=configured,
        )
    return "off"


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
