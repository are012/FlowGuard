from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier, Event, Lock
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from flowguard.services.analysis import AnalysisOrchestrator
from flowguard.services.analysis_support import AIInterpretationClient, InterpretationOutcome
from flowguard.services.data import DataService
from flowguard.storage import FlowGuardRepository

SAMPLE_CSV = (
    Path(__file__).parents[2]
    / "web"
    / "public"
    / "samples"
    / "flowguard-synthetic-transactions.csv"
)


def request_payload() -> dict[str, Any]:
    return {
        "schemaVersion": "1.1",
        "contractVersion": "1.1",
        "promptVersion": "3",
        "requestId": "ai-request-001",
        "idempotencyKey": "analysis-001:rev-7:contract-1.1:prompt-3:ko-KR",
        "analysisId": "analysis-001",
        "snapshotId": "snapshot-007",
        "snapshotRevision": "rev-7",
        "locale": "ko-KR",
        "calculatedAt": "2026-07-31T12:00:00+09:00",
        "facts": {
            "safeToSpend": 180000,
            "nextRisk": {
                "type": "PAYMENT_ACCOUNT",
                "date": "2026-08-03",
                "shortageAmount": 240000,
            },
            "cashflowSummary": {
                "lowestBalance": -240000,
                "lowestBalanceDate": "2026-08-03",
            },
        },
        "evidence": [],
        "actionCandidates": [
            {"actionId": "transfer-1", "type": "TRANSFER", "amount": 240000, "feasible": True}
        ],
    }


def response_payload(**changes: Any) -> dict[str, Any]:
    request = request_payload()
    response = {
        key: request[key]
        for key in (
            "schemaVersion",
            "contractVersion",
            "promptVersion",
            "requestId",
            "idempotencyKey",
            "analysisId",
            "snapshotId",
            "snapshotRevision",
            "locale",
        )
    }
    response.update(
        {
            "riskExplanation": "결제계좌 잔액이 부족할 수 있습니다.",
            "rankedActions": [
                {"actionId": "transfer-1", "priority": 1, "reason": "검증된 이체입니다."}
            ],
            "userMessage": "검증된 대응안을 확인해 주세요.",
            **changes,
        }
    )
    return response


def client_for(handler: httpx.MockTransport) -> AIInterpretationClient:
    return AIInterpretationClient(
        base_url="http://ai.test",
        connect_timeout_seconds=0.01,
        read_timeout_seconds=0.01,
        total_timeout_seconds=0.1,
        transport=handler,
    )


def test_concurrent_identical_analyses_share_one_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FlowGuardRepository("sqlite:///:memory:")
    orchestrator = AnalysisOrchestrator(repository)
    callers_ready = Barrier(5)
    owner_started = Event()
    release_owner = Event()
    counter_lock = Lock()
    execution_count = 0

    def current_revision(_user_id: str) -> str:
        callers_ready.wait(timeout=2)
        return "rev-7"

    def run_once(
        user_id: str,
        *,
        trigger_type: str,
        as_of: datetime | None,
    ) -> dict[str, Any]:
        nonlocal execution_count
        with counter_lock:
            execution_count += 1
        owner_started.set()
        assert release_owner.wait(timeout=2)
        return {
            "analysis_id": "analysis-shared",
            "user_id": user_id,
            "trigger_type": trigger_type,
            "as_of": as_of.isoformat() if as_of is not None else None,
            "status": "COMPLETED",
        }

    monkeypatch.setattr(repository, "current_state_revision", current_revision)
    monkeypatch.setattr(orchestrator, "_run_once", run_once)
    as_of = datetime.fromisoformat("2026-07-31T09:00:00+09:00")

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(
                orchestrator.run,
                "user-1",
                trigger_type="DATA_REFRESH",
                as_of=as_of,
            )
            for _ in range(5)
        ]
        assert owner_started.wait(timeout=2)
        time.sleep(0.05)
        release_owner.set()
        responses = [future.result(timeout=2) for future in futures]

    assert execution_count == 1
    assert {response["analysis_id"] for response in responses} == {"analysis-shared"}
    assert len({json.dumps(response, sort_keys=True) for response in responses}) == 1


def test_concurrent_full_analyses_persist_one_run_and_call_ai_once(tmp_path: Path) -> None:
    class BlockingAIClient:
        def __init__(self) -> None:
            self.calls = 0
            self.started = Event()
            self.release = Event()
            self.lock = Lock()

        def interpret(self, _payload: dict[str, Any]) -> InterpretationOutcome:
            with self.lock:
                self.calls += 1
            self.started.set()
            assert self.release.wait(timeout=5)
            return InterpretationOutcome(
                status="FALLBACK",
                response_payload={},
                attempt_count=1,
                latency_ms=1,
                error_code="test_fallback",
            )

    database_url = f"sqlite:///{tmp_path / 'concurrent-analysis.db'}"
    repository = FlowGuardRepository(database_url)
    DataService(repository).import_csv("user-1", SAMPLE_CSV.read_bytes())
    ai_client = BlockingAIClient()
    orchestrator = AnalysisOrchestrator(repository, ai_client=ai_client)  # type: ignore[arg-type]
    as_of = datetime.fromisoformat("2026-07-31T09:00:00+09:00")

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(
                orchestrator.run,
                "user-1",
                trigger_type="DATA_REFRESH",
                as_of=as_of,
            )
            for _ in range(5)
        ]
        assert ai_client.started.wait(timeout=5)
        time.sleep(0.05)
        ai_client.release.set()
        responses = [future.result(timeout=10) for future in futures]

    with repository.engine.connect() as connection:
        analysis_count = connection.execute(text("SELECT COUNT(*) FROM analysis_runs")).scalar_one()

    assert analysis_count == 1
    assert ai_client.calls == 1
    assert {response["analysis_id"] for response in responses} == {responses[0]["analysis_id"]}
    assert all(response["status"] == "COMPLETED" for response in responses)


def test_completed_analysis_does_not_block_a_later_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = AnalysisOrchestrator(FlowGuardRepository("sqlite:///:memory:"))
    execution_count = 0

    def run_once(
        _user_id: str,
        *,
        trigger_type: str,
        as_of: datetime | None,
    ) -> dict[str, Any]:
        nonlocal execution_count
        execution_count += 1
        return {
            "analysis_id": f"analysis-{execution_count}",
            "trigger_type": trigger_type,
            "as_of": as_of.isoformat() if as_of is not None else None,
        }

    monkeypatch.setattr(orchestrator, "_run_once", run_once)

    first = orchestrator.run("user-1")
    second = orchestrator.run("user-1")

    assert execution_count == 2
    assert first["analysis_id"] != second["analysis_id"]


def test_different_explicit_analysis_times_are_not_coalesced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = AnalysisOrchestrator(FlowGuardRepository("sqlite:///:memory:"))
    owners_ready = Barrier(2)
    counter_lock = Lock()
    execution_count = 0

    def run_once(
        _user_id: str,
        *,
        trigger_type: str,
        as_of: datetime | None,
    ) -> dict[str, Any]:
        nonlocal execution_count
        with counter_lock:
            execution_count += 1
        owners_ready.wait(timeout=2)
        return {
            "analysis_id": f"analysis-{as_of.isoformat() if as_of is not None else 'now'}",
            "trigger_type": trigger_type,
        }

    monkeypatch.setattr(orchestrator, "_run_once", run_once)
    first_as_of = datetime.fromisoformat("2026-07-31T09:00:00+09:00")
    second_as_of = datetime.fromisoformat("2026-08-01T09:00:00+09:00")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(orchestrator.run, "user-1", as_of=first_as_of)
        second = executor.submit(orchestrator.run, "user-1", as_of=second_as_of)
        analysis_ids = {
            first.result(timeout=2)["analysis_id"],
            second.result(timeout=2)["analysis_id"],
        }

    assert execution_count == 2
    assert len(analysis_ids) == 2


def test_ai_request_identifiers_are_stable_for_same_logical_input() -> None:
    orchestrator = AnalysisOrchestrator(FlowGuardRepository("sqlite:///:memory:"))
    report = {
        "created_at": "2026-07-31T12:00:00+09:00",
        "safe_to_spend": {"safe_to_spend": 180_000},
        "risk_metrics": {
            "first_risk_date": "2026-08-03",
            "expected_gap_max": 240_000,
            "shortfall_type": "PAYMENT_ACCOUNT_SHORTAGE",
        },
        "cashflow": {"daily_positions": [{"date": "2026-08-03", "available_balance": -240_000}]},
        "agent": {"gathered_evidence": [], "actionCandidates": []},
    }

    first = orchestrator._build_ai_request_payload(  # noqa: SLF001
        analysis_id="analysis-001",
        snapshot_id="snapshot-007",
        snapshot_revision="rev-7",
        report=report,
    )
    second = orchestrator._build_ai_request_payload(  # noqa: SLF001
        analysis_id="analysis-001",
        snapshot_id="snapshot-007",
        snapshot_revision="rev-7",
        report=report,
    )

    assert first is not None
    assert second is not None
    assert first["idempotencyKey"] == second["idempotencyKey"]
    assert first["requestId"] == second["requestId"]


def test_existing_pending_interpretation_is_not_executed_or_finalized_twice() -> None:
    class MustNotCallAI:
        @staticmethod
        def interpret(_payload: dict[str, Any]) -> None:
            raise AssertionError("the existing interpretation owner must make the call")

    repository = FlowGuardRepository("sqlite:///:memory:")
    snapshot = repository.create_snapshot(
        "user-1",
        as_of=datetime.fromisoformat("2026-07-31T12:00:00+09:00"),
    )
    analysis = repository.create_analysis("user-1", trigger_type="MANUAL")
    for status in (
        "SNAPSHOT_BUILDING",
        "BASELINE_ANALYZING",
        "AGENT_INVESTIGATING",
        "PLAN_EVALUATING",
        "REPORT_BUILDING",
    ):
        analysis = repository.transition_analysis(
            analysis["analysis_id"],
            status,
            message=status,
            snapshot_id=snapshot["snapshot_id"] if status == "SNAPSHOT_BUILDING" else None,
        )
    payload = request_payload()
    payload.update(
        {
            "analysisId": analysis["analysis_id"],
            "snapshotId": snapshot["snapshot_id"],
        }
    )
    repository.create_or_get_interpretation_run(
        analysis_id=payload["analysisId"],
        snapshot_id=payload["snapshotId"],
        snapshot_revision=payload["snapshotRevision"],
        request_id=payload["requestId"],
        idempotency_key=payload["idempotencyKey"],
        contract_version=payload["contractVersion"],
        prompt_version=payload["promptVersion"],
    )
    repository.transition_analysis(
        analysis["analysis_id"],
        "INTERPRETATION_REQUESTING",
        message="requesting",
    )
    orchestrator = AnalysisOrchestrator(
        repository,
        ai_client=MustNotCallAI(),  # type: ignore[arg-type]
    )

    outcome = orchestrator._interpret_analysis(payload)  # noqa: SLF001

    assert outcome["_pending"] is True
    current = repository.get_analysis(analysis["analysis_id"])
    assert current["status"] == "INTERPRETATION_REQUESTING"
    assert current["analysis_status"] == "RUNNING"
    assert current["interpretation_status"] == "RUNNING"


def test_ai_interpretation_client_retries_connection_failure_once() -> None:
    calls = 0

    def fail(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("unavailable", request=request)

    outcome = client_for(httpx.MockTransport(fail)).interpret(request_payload())

    assert outcome.status == "FALLBACK"
    assert outcome.error_code == "connection_failed"
    assert outcome.attempt_count == 2
    assert calls == 2


def test_ai_interpretation_client_falls_back_on_invalid_payload_without_request() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response_payload())

    outcome = client_for(httpx.MockTransport(handler)).interpret({"facts": {"safeToSpend": 180000}})

    assert outcome.status == "FALLBACK"
    assert outcome.error_code == "invalid_payload"
    assert outcome.attempt_count == 0
    assert calls == 0


@pytest.mark.parametrize("status_code", [429, 502, 503, 504])
def test_retryable_status_is_attempted_exactly_twice(status_code: int) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code)

    outcome = client_for(httpx.MockTransport(handler)).interpret(request_payload())

    assert outcome.status == "FALLBACK"
    assert outcome.error_code == f"http_{status_code}"
    assert outcome.attempt_count == 2
    assert calls == 2


def test_retry_reuses_identical_contract_identifiers() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=response_payload())

    outcome = client_for(httpx.MockTransport(handler)).interpret(request_payload())

    assert outcome.status == "SUCCEEDED"
    assert outcome.attempt_count == 2
    assert bodies[0] == bodies[1]


def test_infeasible_action_cannot_be_ranked() -> None:
    payload = request_payload()
    payload["actionCandidates"][0]["feasible"] = False

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_payload())

    outcome = client_for(httpx.MockTransport(handler)).interpret(payload)

    assert outcome.status == "FALLBACK"
    assert outcome.error_code == "unknown_ranked_action"
    assert outcome.attempt_count == 1


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        (httpx.Response(400), "http_400"),
        (httpx.Response(200, content=b"not-json"), "invalid_json"),
        (
            httpx.Response(200, json=response_payload(requestId="different")),
            "requestId_mismatch",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(
                    rankedActions=[{"actionId": "unknown-1", "priority": 1, "reason": "알 수 없음"}]
                ),
            ),
            "unknown_ranked_action",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(riskExplanation="제공되지 않은 999,999원 위험을 설명합니다."),
            ),
            "unknown_amount_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(riskExplanation="입금이 14일 늦어집니다."),
            ),
            "unknown_duration_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(riskExplanation="위험 확률은 92%입니다."),
            ),
            "unknown_percentage_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(riskExplanation="부족액은 370,000입니다."),
            ),
            "unknown_amount_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(riskExplanation="부족액은 370,000."),
            ),
            "unknown_amount_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(riskExplanation="부족액은 370000입니다."),
            ),
            "unknown_numeric_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(riskExplanation="부족액은 370000."),
            ),
            "unknown_numeric_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(riskExplanation="부족액은 9999입니다."),
            ),
            "unknown_numeric_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(riskExplanation="부족액은 17~24만원입니다."),
            ),
            "unknown_amount_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(riskExplanation="부족액은 17-24만원입니다."),
            ),
            "unknown_amount_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(riskExplanation="부족액은 17에서 24만원입니다."),
            ),
            "unknown_amount_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(riskExplanation="1억원이 부족합니다."),
            ),
            "unknown_amount_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(riskExplanation="-180,000원이 남습니다."),
            ),
            "unknown_amount_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(userMessage="2027-01-01에 조치해 주세요."),
            ),
            "unknown_date_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(userMessage="2027. 1. 1.에 조치해 주세요."),
            ),
            "unknown_date_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(userMessage="2027-1-1에 조치해 주세요."),
            ),
            "unknown_date_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(userMessage="2027/1/1에 조치해 주세요."),
            ),
            "unknown_date_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(userMessage="9. 9.에 조치해 주세요."),
            ),
            "unknown_numeric_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(riskExplanation="삼십칠만 원이 부족합니다."),
            ),
            "unknown_amount_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(riskExplanation="정시 지급률은 팔십일 퍼센트입니다."),
            ),
            "unknown_percentage_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(riskExplanation="입금이 평균 열나흘 늦어집니다."),
            ),
            "unknown_duration_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(riskExplanation="입금이 평균 3주 늦어집니다."),
            ),
            "unknown_numeric_claim",
        ),
    ],
)
def test_non_retryable_response_falls_back_without_retry(
    response: httpx.Response,
    error_code: str,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response

    outcome = client_for(httpx.MockTransport(handler)).interpret(request_payload())

    assert outcome.status == "FALLBACK"
    assert outcome.error_code == error_code
    assert outcome.attempt_count == 1
    assert calls == 1


@pytest.mark.parametrize(
    ("candidate_amount", "evidence", "risk_explanation"),
    [
        (240_000, [], "24만원이 부족합니다."),
        (240_000, [], "부족액은 240,000입니다."),
        (10_000_000, [], "1,000만원이 부족합니다."),
        (100_000_000, [], "1억원이 부족합니다."),
        (240_000, [], "최저 잔액은 -240,000원입니다."),
        (240_000, [{"average_delay_days": 14}], "입금이 평균 14일 늦어집니다."),
        (240_000, [{"on_time_rate": 0.92}], "정시 지급률은 92%입니다."),
        (240_000, [{"on_time_rate": 0.92}], "정시 지급률은 0.92입니다."),
        (240_000, [{"payment_history_count": 1_000}], "지급 이력은 1,000건입니다."),
        (240_000, [], "제1원인은 결제계좌 잔액 부족입니다."),
    ],
)
def test_supplied_numeric_claim_is_allowed(
    candidate_amount: int,
    evidence: list[dict[str, Any]],
    risk_explanation: str,
) -> None:
    payload = request_payload()
    payload["actionCandidates"][0]["amount"] = candidate_amount
    payload["facts"]["nextRisk"]["shortageAmount"] = candidate_amount
    payload["facts"]["cashflowSummary"]["lowestBalance"] = -candidate_amount
    payload["evidence"] = evidence

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=response_payload(riskExplanation=risk_explanation),
        )

    outcome = client_for(httpx.MockTransport(handler)).interpret(payload)

    assert outcome.status == "SUCCEEDED"
    assert outcome.error_code is None


@pytest.mark.parametrize(
    ("evidence", "risk_explanation", "error_code"),
    [
        ([{"payment_history_count": 4}], "4원이 부족합니다.", "unknown_amount_claim"),
        (
            [{"maximum_delay_days": 7}],
            "위험 확률은 7%입니다.",
            "unknown_percentage_claim",
        ),
        (
            [{"maximum_delay_days": 7}],
            "입금이 최대 -7일 늦어집니다.",
            "unknown_duration_claim",
        ),
        (
            [{"maximum_delay_days": 14}],
            "입금이 평균 14일 늦어집니다.",
            "unknown_duration_claim",
        ),
        (
            [{"maximum_delay_days": 14}],
            "입금이 최대 7~14일 늦어집니다.",
            "unknown_duration_claim",
        ),
        (
            [{"average_delay_days": 14}],
            "평균이 아니라 최대 지연은 14일입니다.",
            "unknown_duration_claim",
        ),
        (
            [{"on_time_rate": 0.92}],
            "위험 확률은 92%입니다.",
            "unknown_percentage_claim",
        ),
        (
            [{"on_time_rate": 0.92}],
            "정시 지급률은 아니지만 위험 확률은 92%입니다.",
            "unknown_percentage_claim",
        ),
        (
            [{"on_time_rate": 0.92}],
            "정시 지급률은 92%p 상승했습니다.",
            "unknown_percentage_claim",
        ),
        (
            [{"on_time_rate": 0.92}],
            "정시 지급률은 92퍼센트포인트 상승했습니다.",
            "unknown_percentage_claim",
        ),
        (
            [{"on_time_rate": 0.92}],
            "정시 지급률은 92%포인트 상승했습니다.",
            "unknown_percentage_claim",
        ),
        (
            [{"on_time_rate": 0.92}],
            "정시 지급률은 0.81입니다.",
            "unknown_percentage_claim",
        ),
    ],
)
def test_numeric_claim_cannot_borrow_a_value_from_another_unit(
    evidence: list[dict[str, Any]],
    risk_explanation: str,
    error_code: str,
) -> None:
    payload = request_payload()
    payload["evidence"] = evidence

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=response_payload(riskExplanation=risk_explanation),
        )

    outcome = client_for(httpx.MockTransport(handler)).interpret(payload)

    assert outcome.status == "FALLBACK"
    assert outcome.error_code == error_code
    assert outcome.attempt_count == 1


@pytest.mark.parametrize(
    "risk_explanation",
    [
        "1억2천만원이 부족합니다.",
        "1백만원이 부족합니다.",
    ],
)
def test_partially_parsed_korean_amount_is_rejected(risk_explanation: str) -> None:
    payload = request_payload()
    payload["actionCandidates"][0]["amount"] = 100_000_000

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=response_payload(riskExplanation=risk_explanation),
        )

    outcome = client_for(httpx.MockTransport(handler)).interpret(payload)

    assert outcome.status == "FALLBACK"
    assert outcome.error_code == "unknown_amount_claim"
    assert outcome.attempt_count == 1


def test_supplied_korean_date_is_not_treated_as_a_day_count() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=response_payload(userMessage="8월 3 일에 조치해 주세요."),
        )

    outcome = client_for(httpx.MockTransport(handler)).interpret(request_payload())

    assert outcome.status == "SUCCEEDED"
    assert outcome.error_code is None


@pytest.mark.parametrize(
    "user_message",
    [
        "2026. 8. 3.에 조치해 주세요.",
        "2026/8/3에 조치해 주세요.",
        "2026-8-3에 조치해 주세요.",
    ],
)
def test_supplied_delimited_date_is_allowed(user_message: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=response_payload(userMessage=user_message),
        )

    outcome = client_for(httpx.MockTransport(handler)).interpret(request_payload())

    assert outcome.status == "SUCCEEDED"
    assert outcome.error_code is None


def test_infeasible_candidate_amount_is_not_an_allowed_claim() -> None:
    payload = request_payload()
    payload["actionCandidates"].append(
        {"actionId": "blocked-1", "type": "TRANSFER", "amount": 999_999, "feasible": False}
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=response_payload(riskExplanation="999,999원이 필요합니다."),
        )

    outcome = client_for(httpx.MockTransport(handler)).interpret(payload)

    assert outcome.status == "FALLBACK"
    assert outcome.error_code == "unknown_amount_claim"


@pytest.mark.parametrize("field", ["counterparty_id", "candidate"])
def test_identifier_date_fragment_is_not_an_allowed_claim(field: str) -> None:
    payload = request_payload()
    payload["evidence"] = [{field: "client-2027-01-01"}]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=response_payload(userMessage="2027-01-01에 조치해 주세요."),
        )

    outcome = client_for(httpx.MockTransport(handler)).interpret(payload)

    assert outcome.status == "FALLBACK"
    assert outcome.error_code == "unknown_date_claim"
