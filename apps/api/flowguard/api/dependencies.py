"""FastAPI dependencies backed by application-scoped services."""

from __future__ import annotations

import os
from typing import Annotated
from uuid import uuid4

from fastapi import Header, Request

from flowguard.services.analysis import AnalysisOrchestrator
from flowguard.services.data import DataService
from flowguard.services.recommendations import RecommendationService
from flowguard.services.reports import ReportQueryService
from flowguard.storage import FlowGuardRepository


def get_user_id(
    x_user_id: Annotated[str | None, Header(alias="X-User-ID")] = None,
) -> str:
    user_id = x_user_id or os.getenv("FLOWGUARD_DEMO_USER_ID", "demo-user")
    return user_id.strip()


def get_request_id(
    request: Request,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> str:
    state_request_id = getattr(request.state, "request_id", None)
    if isinstance(state_request_id, str) and state_request_id.strip():
        return state_request_id.strip()
    if isinstance(x_request_id, str) and x_request_id.strip():
        return x_request_id.strip()
    return f"req-{uuid4()}"


def get_repository(request: Request) -> FlowGuardRepository:
    return request.app.state.repository


def get_data_service(request: Request) -> DataService:
    return request.app.state.data_service


def get_analysis_service(request: Request) -> AnalysisOrchestrator:
    return request.app.state.analysis_service


def get_recommendation_service(request: Request) -> RecommendationService:
    return request.app.state.recommendation_service


def get_report_service(request: Request) -> ReportQueryService:
    return request.app.state.report_service
