from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flowguard.main import create_app
from flowguard.services.agent_factory import build_investigator
from flowguard.services.analysis import AnalysisOrchestrator
from flowguard.services.investigator import LiquidityInvestigator
from flowguard.services.luna import LunaLiquidityInvestigator
from flowguard.services.recommendations import RecommendationService
from flowguard.services.tools import CoreToolService
from flowguard.storage import FlowGuardRepository

AS_OF = "2026-07-24T09:00:00+09:00"


class ScriptedResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.candidate_id: str | None = None

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        step = len(self.calls)
        if step == 1:
            return self._function_call("get_financial_context", {})
        if step == 2:
            return self._function_call("list_action_candidates", {})

        candidate_id = self._candidate_id(kwargs["input"])
        self.candidate_id = candidate_id
        if step == 3:
            return self._function_call(
                "evaluate_action_candidate",
                {"candidate_id": candidate_id},
            )
        return self._function_call(
            "submit_decision",
            {
                "risk_hypotheses": [
                    {
                        "type": "PAYMENT_ACCOUNT",
                        "summary": "결제 예정액보다 결제계좌 잔액이 부족합니다.",
                        "evidence_ids": ["bill"],
                    }
                ],
                "selected_candidate_id": candidate_id,
                "alternative_candidate_ids": [],
                "recommendation_summary": "예비 계좌에서 결제계좌로 이체합니다.",
                "selection_reason": "가상 적용과 금융 안전정책 검증을 통과했습니다.",
                "evidence_summary": ["결제계좌 부족액과 이체 가능 잔액을 확인했습니다."],
                "unresolved_questions": [],
            },
        )

    @staticmethod
    def _candidate_id(input_items: list[Any]) -> str:
        for item in reversed(input_items):
            if not isinstance(item, dict) or item.get("type") != "function_call_output":
                continue
            payload = json.loads(item["output"])
            candidates = payload.get("candidates")
            if candidates:
                return str(candidates[0]["candidate_id"])
        raise AssertionError("Luna에 전달된 후보 대응안이 없습니다.")

    @staticmethod
    def _function_call(name: str, arguments: dict[str, Any]) -> SimpleNamespace:
        call_id = f"call-{name}"
        return SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="function_call",
                    name=name,
                    arguments=json.dumps(arguments, ensure_ascii=False),
                    call_id=call_id,
                )
            ],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        )


class ScriptedClient:
    def __init__(self) -> None:
        self.responses = ScriptedResponses()


class FailingResponses:
    @staticmethod
    def create(**_kwargs: Any) -> None:
        raise RuntimeError("model unavailable")


class FailingClient:
    responses = FailingResponses()


def _create_luna_app(
    client: ScriptedClient | FailingClient,
) -> tuple[Any, FlowGuardRepository]:
    repository = FlowGuardRepository("sqlite:///:memory:")
    app = create_app(repository)
    tools = CoreToolService(repository)
    investigator = LunaLiquidityInvestigator(repository, tools, client)
    analyses = AnalysisOrchestrator(repository, tools=tools, investigator=investigator)
    app.state.analysis_service = analyses
    app.state.recommendation_service = RecommendationService(repository, tools, analyses)
    return app, repository


def _seed_payment_risk(client: TestClient, *, user_id: str) -> None:
    headers = {"X-User-ID": user_id}
    for account_id, balance, payment in (
        ("payment", 50_000, True),
        ("reserve", 200_000, False),
    ):
        response = client.post(
            "/api/v1/accounts",
            headers=headers,
            json={
                "account_id": account_id,
                "name": account_id,
                "account_type": "CHECKING",
                "balance": balance,
                "is_payment_account": payment,
                "is_available_for_transfer": True,
            },
        )
        assert response.status_code == 201
    event = client.post(
        "/api/v1/scheduled-events",
        headers=headers,
        json={
            "event_id": "bill",
            "event_type": "CARD_BILL",
            "direction": "OUTFLOW",
            "amount": 100_000,
            "expected_date": "2026-07-25",
            "account_id": "payment",
            "certainty": "CONFIRMED",
            "is_essential": True,
        },
    )
    assert event.status_code == 201


def _run_analysis(client: TestClient, *, user_id: str) -> dict[str, Any]:
    response = client.post(
        "/api/v1/analyses",
        headers={"X-User-ID": user_id},
        json={"trigger_type": "DATA_REFRESH", "as_of": AS_OF},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "COMPLETED"
    dashboard = client.get(
        "/api/v1/dashboard",
        headers={"X-User-ID": user_id},
    )
    assert dashboard.status_code == 200
    return dashboard.json()


def test_auto_mode_without_api_key_exposes_keyless_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLOWGUARD_AGENT_MODE", "auto")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    repository = FlowGuardRepository("sqlite:///:memory:")
    app = create_app(repository)

    with TestClient(app, raise_server_exceptions=False) as client:
        _seed_payment_risk(client, user_id="keyless-user")
        dashboard = _run_analysis(client, user_id="keyless-user")

    trace = dashboard["decision_trace"]
    assert trace["mode"] == "DETERMINISTIC_FALLBACK"
    assert trace["model"] == "gpt-5.6-luna"
    assert trace["fallback_reason"] == "OPENAI_API_KEY_NOT_CONFIGURED"
    assert dashboard["recommendation"]["actions"][0]["type"] == "transfer"
    metadata = repository.latest_analysis("keyless-user")["metadata"]
    assert metadata["agent_mode"] == "DETERMINISTIC_FALLBACK"
    assert metadata["agent_model"] == "gpt-5.6-luna"


def test_factory_uses_injected_luna_client_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLOWGUARD_AGENT_MODE", "auto")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    repository = FlowGuardRepository("sqlite:///:memory:")
    tools = CoreToolService(repository)

    investigator = build_investigator(repository, tools, client=ScriptedClient())

    assert isinstance(investigator, LunaLiquidityInvestigator)
    assert not isinstance(investigator, LiquidityInvestigator)


def test_luna_tool_loop_persists_public_decision_trace() -> None:
    scripted_client = ScriptedClient()
    app, repository = _create_luna_app(scripted_client)

    with TestClient(app, raise_server_exceptions=False) as client:
        _seed_payment_risk(client, user_id="luna-user")
        dashboard = _run_analysis(client, user_id="luna-user")

    trace = dashboard["decision_trace"]
    assert trace["mode"] == "LUNA"
    assert trace["model"] == "gpt-5.6-luna"
    assert trace["usage"] == {
        "input_tokens": 40,
        "output_tokens": 20,
        "total_tokens": 60,
    }
    assert {step["kind"] for step in trace["steps"]} == {
        "RISK_HYPOTHESIS",
        "TOOL_CALL",
        "CANDIDATE_EVALUATION",
        "FINAL_SELECTION",
    }
    assert "chain_of_thought" not in json.dumps(trace)
    assert dashboard["recommendation"]["actions"][0]["type"] == "transfer"

    requests = scripted_client.responses.calls
    assert [request["model"] for request in requests] == ["gpt-5.6-luna"] * 4
    assert all(request["store"] is False for request in requests)
    assert all(request["parallel_tool_calls"] is False for request in requests)
    assert all(request["reasoning"]["context"] == "current_turn" for request in requests)
    assert all(tool["strict"] is True for tool in requests[0]["tools"])

    report = repository.latest_report("luna-user")
    assert report["agent"]["recommended_plan"]["selected_by"] == "gpt-5.6-luna"
    assert report["versions"]["agent_mode"] == "LUNA"
    assert report["versions"]["agent_model"] == "gpt-5.6-luna"
    assert report["versions"]["agent_prompt_version"] == "luna-investigator-1"
    assert scripted_client.responses.candidate_id is not None


def test_luna_failure_falls_back_to_validated_deterministic_plan() -> None:
    app, _repository = _create_luna_app(FailingClient())

    with TestClient(app, raise_server_exceptions=False) as client:
        _seed_payment_risk(client, user_id="fallback-user")
        dashboard = _run_analysis(client, user_id="fallback-user")

    trace = dashboard["decision_trace"]
    assert trace["mode"] == "DETERMINISTIC_FALLBACK"
    assert trace["fallback_reason"] == "LUNA_API_OR_PROTOCOL_ERROR:RuntimeError"
    assert dashboard["recommendation"]["actions"][0]["type"] == "transfer"
