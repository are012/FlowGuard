"""Versioned REST endpoints defined by the FlowGuard specification."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, UploadFile, status

from flowguard.services.analysis import AnalysisOrchestrator
from flowguard.services.data import DataService
from flowguard.services.recommendations import RecommendationService
from flowguard.services.reports import ReportQueryService
from flowguard.storage import FlowGuardRepository, RecordNotFound

from .dependencies import (
    get_analysis_service,
    get_data_service,
    get_recommendation_service,
    get_report_service,
    get_repository,
    get_request_id,
    get_user_id,
)
from .schemas import (
    AccountCreate,
    AccountPatch,
    AnalysisRequest,
    CandidateDecision,
    CounterpartyCreate,
    CounterpartyPatch,
    InstallmentPrecheckRequest,
    ReceivableCreate,
    ReceivablePatch,
    RecommendationDecision,
    ScheduledEventCreate,
    ScheduledEventPatch,
    TransactionPatch,
    UserPreferencesRequest,
    dumped,
)

router = APIRouter()
api = APIRouter(prefix="/api/v1")

UserId = Annotated[str, Depends(get_user_id)]
RequestId = Annotated[str, Depends(get_request_id)]
Data = Annotated[DataService, Depends(get_data_service)]
Analyses = Annotated[AnalysisOrchestrator, Depends(get_analysis_service)]
Recommendations = Annotated[RecommendationService, Depends(get_recommendation_service)]
Reports = Annotated[ReportQueryService, Depends(get_report_service)]
Repository = Annotated[FlowGuardRepository, Depends(get_repository)]


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@api.post("/imports/transactions", status_code=status.HTTP_201_CREATED)
async def import_transactions(
    user_id: UserId,
    data: Data,
    analyses: Analyses,
    file: Annotated[UploadFile, File(description="UTF-8 FlowGuard CSV")],
    protection_level: Annotated[float | None, Form(ge=0, le=1)] = None,
    minimum_total_reserve: Annotated[int | None, Form(ge=0)] = None,
    income_type: Annotated[str | None, Form(max_length=100)] = None,
) -> dict[str, Any]:
    content = await file.read()
    max_bytes = int(os.getenv("FLOWGUARD_MAX_CSV_BYTES", str(5 * 1024 * 1024)))
    if len(content) > max_bytes:
        from flowguard.services.errors import ServiceError

        raise ServiceError(
            "INVALID_CSV_FORMAT",
            "CSV 파일 크기 제한을 초과했습니다.",
            details={"max_bytes": max_bytes},
            http_status=413,
        )
    imported = data.import_csv(user_id, content)
    preference_changes = {
        key: value
        for key, value in {
            "protection_level": protection_level,
            "minimum_total_reserve": minimum_total_reserve,
            "income_type": income_type,
        }.items()
        if value is not None
    }
    if preference_changes:
        imported["preferences"] = data.update_preferences(user_id, preference_changes)
    imported["revision"] = analyses.repository.current_state_revision(user_id)
    imported["analysis_required"] = True
    return imported


@api.get("/accounts")
def list_accounts(user_id: UserId, data: Data) -> list[dict[str, Any]]:
    return data.list_records(user_id, "accounts")


@api.post("/accounts", status_code=status.HTTP_201_CREATED)
def create_account(
    body: AccountCreate, user_id: UserId, data: Data, analyses: Analyses
) -> dict[str, Any]:
    payload = dumped(body)
    payload.setdefault("updated_at", _now().isoformat())
    return _mutation_result(data.create_record(user_id, "accounts", payload), user_id, analyses)


@api.patch("/accounts/{account_id}")
def patch_account(
    account_id: str,
    body: AccountPatch,
    user_id: UserId,
    data: Data,
    analyses: Analyses,
) -> dict[str, Any]:
    current = data.get_record(user_id, "accounts", account_id)
    changes = dumped(body, exclude_unset=True)
    validated = AccountCreate.model_validate(
        {
            **current,
            **changes,
            "account_id": account_id,
            "updated_at": _now(),
        }
    )
    return _mutation_result(
        data.patch_record(
            user_id,
            "accounts",
            account_id,
            dumped(validated),
        ),
        user_id,
        analyses,
    )


@api.get("/cards")
def list_cards(user_id: UserId, data: Data) -> list[dict[str, Any]]:
    return data.list_records(user_id, "cards")


@api.get("/transactions")
def list_transactions(user_id: UserId, data: Data) -> list[dict[str, Any]]:
    return data.list_records(user_id, "transactions")


@api.patch("/transactions/{transaction_id}")
def patch_transaction(
    transaction_id: str,
    body: TransactionPatch,
    user_id: UserId,
    data: Data,
    analyses: Analyses,
) -> dict[str, Any]:
    return _mutation_result(
        data.patch_record(
            user_id,
            "transactions",
            transaction_id,
            dumped(body, exclude_unset=True),
        ),
        user_id,
        analyses,
    )


@api.get("/counterparties")
def list_counterparties(user_id: UserId, data: Data) -> list[dict[str, Any]]:
    return data.list_records(user_id, "counterparties")


@api.post("/counterparties", status_code=status.HTTP_201_CREATED)
def create_counterparty(
    body: CounterpartyCreate,
    user_id: UserId,
    data: Data,
    analyses: Analyses,
) -> dict[str, Any]:
    payload = dumped(body)
    payload.setdefault("updated_at", _now().isoformat())
    return _mutation_result(
        data.create_record(user_id, "counterparties", payload),
        user_id,
        analyses,
    )


@api.patch("/counterparties/{counterparty_id}")
def patch_counterparty(
    counterparty_id: str,
    body: CounterpartyPatch,
    user_id: UserId,
    data: Data,
    analyses: Analyses,
) -> dict[str, Any]:
    changes = dumped(body, exclude_unset=True)
    changes["updated_at"] = _now().isoformat()
    return _mutation_result(
        data.patch_record(user_id, "counterparties", counterparty_id, changes),
        user_id,
        analyses,
    )


@api.get("/scheduled-events")
def list_scheduled_events(user_id: UserId, data: Data) -> list[dict[str, Any]]:
    return data.list_records(user_id, "scheduled_events")


@api.post("/scheduled-events", status_code=status.HTTP_201_CREATED)
def create_scheduled_event(
    body: ScheduledEventCreate,
    user_id: UserId,
    data: Data,
    analyses: Analyses,
) -> dict[str, Any]:
    data.get_record(user_id, "accounts", body.account_id)
    return _mutation_result(
        data.create_record(user_id, "scheduled_events", dumped(body)),
        user_id,
        analyses,
    )


@api.patch("/scheduled-events/{event_id}")
def patch_scheduled_event(
    event_id: str,
    body: ScheduledEventPatch,
    user_id: UserId,
    data: Data,
    analyses: Analyses,
) -> dict[str, Any]:
    if body.account_id is not None:
        data.get_record(user_id, "accounts", body.account_id)
    return _mutation_result(
        data.patch_record(
            user_id,
            "scheduled_events",
            event_id,
            dumped(body, exclude_unset=True),
        ),
        user_id,
        analyses,
    )


@api.get("/candidates")
def list_candidates(user_id: UserId, data: Data) -> list[dict[str, Any]]:
    return data.list_records(user_id, "candidates")


@api.patch("/candidates/{candidate_id}")
def decide_candidate(
    candidate_id: str,
    body: CandidateDecision,
    user_id: UserId,
    data: Data,
    analyses: Analyses,
) -> dict[str, Any]:
    return _mutation_result(
        data.decide_candidate(
            user_id,
            candidate_id,
            decision=body.decision,
            details=body.details,
        ),
        user_id,
        analyses,
    )


@api.get("/preferences")
def get_preferences(user_id: UserId, data: Data) -> dict[str, Any]:
    return data.get_preferences(user_id)


@api.patch("/preferences")
@api.put("/preferences")
def update_preferences(
    body: UserPreferencesRequest,
    user_id: UserId,
    data: Data,
    analyses: Analyses,
) -> dict[str, Any]:
    return _mutation_result(
        data.update_preferences(user_id, dumped(body, exclude_unset=True)),
        user_id,
        analyses,
    )


@api.get("/receivables")
def list_receivables(user_id: UserId, reports: Reports) -> list[dict[str, Any]]:
    return reports.receivables(user_id)


@api.post("/receivables", status_code=status.HTTP_201_CREATED)
def create_receivable(
    body: ReceivableCreate,
    user_id: UserId,
    data: Data,
    analyses: Analyses,
) -> dict[str, Any]:
    data.get_record(user_id, "accounts", body.destination_account_id)
    data.get_record(user_id, "counterparties", body.counterparty_id)
    payload = dumped(body)
    payload.setdefault("updated_at", _now().isoformat())
    return _mutation_result(data.create_record(user_id, "receivables", payload), user_id, analyses)


@api.patch("/receivables/{receivable_id}")
def patch_receivable(
    receivable_id: str,
    body: ReceivablePatch,
    user_id: UserId,
    data: Data,
    analyses: Analyses,
) -> dict[str, Any]:
    if body.destination_account_id is not None:
        data.get_record(user_id, "accounts", body.destination_account_id)
    if body.counterparty_id is not None:
        data.get_record(user_id, "counterparties", body.counterparty_id)
    changes = dumped(body, exclude_unset=True)
    changes["updated_at"] = _now().isoformat()
    return _mutation_result(
        data.patch_record(user_id, "receivables", receivable_id, changes),
        user_id,
        analyses,
    )


@api.get("/installments")
def list_installments(user_id: UserId, data: Data) -> list[dict[str, Any]]:
    return data.list_records(user_id, "installment_plans")


@api.post("/installments/precheck")
def installment_precheck(
    body: InstallmentPrecheckRequest,
    user_id: UserId,
    recommendations: Recommendations,
) -> dict[str, Any]:
    return recommendations.installment_precheck(
        user_id,
        purchase_amount=body.purchase_amount,
        installment_months=body.installment_months,
        first_payment_date=body.first_payment_date,
        card_id=body.card_id,
    )


@api.post("/analyses", status_code=status.HTTP_201_CREATED)
def create_analysis(
    body: AnalysisRequest,
    user_id: UserId,
    request_id: RequestId,
    analyses: Analyses,
) -> dict[str, Any]:
    return analyses.run(
        user_id,
        trigger_type=body.trigger_type,
        as_of=body.as_of,
        request_id=request_id,
    )


@api.get("/analyses/{analysis_id}")
def get_analysis(analysis_id: str, user_id: UserId, repository: Repository) -> dict[str, Any]:
    try:
        analysis = repository.get_analysis(analysis_id)
    except RecordNotFound:
        from flowguard.services.errors import not_found

        raise not_found("analysis", analysis_id) from None
    if analysis["user_id"] != user_id:
        from flowguard.services.errors import not_found

        raise not_found("analysis", analysis_id)
    return analysis


@api.get("/analyses/{analysis_id}/events")
def get_analysis_events(
    analysis_id: str, user_id: UserId, repository: Repository
) -> list[dict[str, Any]]:
    get_analysis(analysis_id, user_id, repository)
    return repository.list_analysis_events(analysis_id)


@api.get("/dashboard")
def dashboard(user_id: UserId, reports: Reports) -> dict[str, Any]:
    return reports.dashboard(user_id)


@api.get("/reports/latest")
def latest_report(user_id: UserId, reports: Reports) -> dict[str, Any]:
    return reports.latest(user_id)


@api.get("/ai/metrics")
def ai_metrics(user_id: UserId, reports: Reports) -> dict[str, Any]:
    return reports.ai_metrics(user_id)


@api.get("/cashflow/timeline")
def cashflow_timeline(user_id: UserId, reports: Reports) -> dict[str, Any]:
    return reports.cashflow_timeline(user_id)


@api.get("/risks/next")
def next_risk(user_id: UserId, reports: Reports) -> dict[str, Any]:
    return reports.next_risk(user_id)


@api.get("/recommendations")
def list_recommendations(user_id: UserId, recommendations: Recommendations) -> list[dict[str, Any]]:
    return recommendations.list(user_id)


@api.get("/recommendations/{recommendation_id}")
def get_recommendation(
    recommendation_id: str,
    user_id: UserId,
    recommendations: Recommendations,
) -> dict[str, Any]:
    return recommendations.get(user_id, recommendation_id)


@api.post("/recommendations/{recommendation_id}/approve")
def approve_recommendation(
    recommendation_id: str,
    body: RecommendationDecision,
    user_id: UserId,
    request_id: RequestId,
    recommendations: Recommendations,
) -> dict[str, Any]:
    return recommendations.approve(
        user_id,
        recommendation_id,
        reason=body.reason,
        request_id=request_id,
    )


@api.post("/recommendations/{recommendation_id}/reject")
def reject_recommendation(
    recommendation_id: str,
    body: RecommendationDecision,
    user_id: UserId,
    recommendations: Recommendations,
) -> dict[str, Any]:
    return recommendations.reject(user_id, recommendation_id, reason=body.reason)


@api.post("/recommendations/{recommendation_id}/alternatives")
def recommendation_alternatives(
    recommendation_id: str,
    body: RecommendationDecision,
    user_id: UserId,
    recommendations: Recommendations,
) -> dict[str, Any]:
    return recommendations.alternatives(user_id, recommendation_id, reason=body.reason)


router.include_router(api)


def _now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Seoul"))


def _mutation_result(
    record: dict[str, Any],
    user_id: str,
    analyses: AnalysisOrchestrator,
) -> dict[str, Any]:
    return {
        **record,
        "revision": analyses.repository.current_state_revision(user_id),
        "analysis_required": True,
    }
