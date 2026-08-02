from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event as ThreadEvent
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flowguard.ai_service import (
    FINAL_CONCLUSION_INSTRUCTION,
    InvestigationConcludeContent,
    InvestigationPlanContent,
    create_app,
)

ECHO_FIELDS = (
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


def envelope(*, idempotency_key: str = "investigation-key") -> dict[str, str]:
    return {
        "schemaVersion": "1.3",
        "contractVersion": "1.3",
        "promptVersion": "invest-1",
        "requestId": "investigation-request-a",
        "idempotencyKey": idempotency_key,
        "analysisId": "analysis-a",
        "snapshotId": "snapshot-a",
        "snapshotRevision": "revision-a",
        "locale": "ko-KR",
    }


def request_base(*, idempotency_key: str = "investigation-key") -> dict[str, Any]:
    return {
        **envelope(idempotency_key=idempotency_key),
        "baseline": {
            "type": "PAYMENT_ACCOUNT",
            "date": "2026-08-25",
            "shortageAmount": 250_000,
        },
        "targets": {
            "counterpartyIds": ["counterparty-a", "counterparty-b"],
            "eventWindow": {"dateFrom": "2026-08-02", "dateTo": "2026-10-31"},
            "actionTypes": ["transfer", "confirm_receivable"],
        },
    }


def plan_request(*, idempotency_key: str = "investigation-key") -> dict[str, Any]:
    return request_base(idempotency_key=idempotency_key)


def investigation(*, counterparty_id: str = "counterparty-a") -> dict[str, Any]:
    return {
        "tool": "get_counterparty_evidence",
        "params": {"counterpartyId": counterparty_id},
        "reason": "입금 예정 거래처의 지급 이력을 확인합니다.",
    }


def conclude_request(
    *,
    idempotency_key: str = "investigation-key",
    allow_additional: bool = True,
) -> dict[str, Any]:
    return {
        **request_base(idempotency_key=idempotency_key),
        "observations": [
            {
                **investigation(),
                "result": {
                    "counterparty_id": "counterparty-a",
                    "payment_history_count": 4,
                },
            }
        ],
        "allowAdditionalInvestigations": allow_additional,
    }


def plan_content() -> dict[str, Any]:
    return {"investigations": [investigation()]}


def conclusion_content() -> dict[str, Any]:
    return {
        "conclusion": {
            "hypotheses": [
                {
                    "type": "COUNTERPARTY_DELAY",
                    "summary": "거래처 지급 변동이 유동성 위험을 키울 수 있습니다.",
                    "priority": 1,
                }
            ],
            "candidatePriorities": ["confirm_receivable", "transfer"],
            "unresolved": ["최근 지급 이력의 대표성을 확인하지 못했습니다."],
        }
    }


class FakeInvestigationResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.plan_output: Any = plan_content()
        self.conclude_output: Any = conclusion_content()
        self.failure: Exception | None = None

    def parse(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        if kwargs["text_format"] is InvestigationPlanContent:
            return SimpleNamespace(output_parsed=deepcopy(self.plan_output))
        if kwargs["text_format"] is InvestigationConcludeContent:
            return SimpleNamespace(output_parsed=deepcopy(self.conclude_output))
        raise AssertionError("unexpected structured response model")


class FakeOpenAI:
    def __init__(self) -> None:
        self.responses = FakeInvestigationResponses()


def test_plan_and_conclude_echo_contract_and_never_give_openai_tools() -> None:
    fake = FakeOpenAI()
    plan_payload = plan_request(idempotency_key="plan-key")
    conclude_payload = conclude_request(idempotency_key="conclude-key")
    with TestClient(create_app(openai_client=fake, model="test-model")) as client:
        planned = client.post("/investigate/plan", json=plan_payload)
        concluded = client.post(
            "/investigate/conclude",
            json=conclude_payload,
        )

    assert planned.status_code == 200
    assert concluded.status_code == 200
    for response, source in ((planned, plan_payload), (concluded, conclude_payload)):
        for field in ECHO_FIELDS:
            assert response.json()[field] == source[field]

    assert len(fake.responses.calls) == 2
    plan_call, conclude_call = fake.responses.calls
    for call in fake.responses.calls:
        assert call["model"] == "test-model"
        assert call["store"] is False
        assert "tools" not in call

    plan_input = json.loads(plan_call["input"])
    assert set(plan_input) == {"locale", "baseline", "targets"}
    conclude_input = json.loads(conclude_call["input"])
    assert set(conclude_input) == {
        "locale",
        "baseline",
        "targets",
        "observations",
        "allowAdditionalInvestigations",
    }
    assert conclude_input["allowAdditionalInvestigations"] is True


def test_final_conclude_requires_conclusion_in_prompt_and_response() -> None:
    fake = FakeOpenAI()
    fake.responses.conclude_output = {
        "additionalInvestigations": [investigation(counterparty_id="counterparty-b")]
    }
    with TestClient(create_app(openai_client=fake)) as client:
        response = client.post(
            "/investigate/conclude",
            json=conclude_request(idempotency_key="final-key", allow_additional=False),
        )

    assert response.status_code == 422
    assert response.json()["detail"] == {"code": "invalid_investigation_response"}
    call = fake.responses.calls[0]
    assert FINAL_CONCLUSION_INSTRUCTION in call["instructions"]
    assert json.loads(call["input"])["allowAdditionalInvestigations"] is False


def test_nonfinal_conclude_accepts_an_additional_investigation() -> None:
    fake = FakeOpenAI()
    fake.responses.conclude_output = {
        "additionalInvestigations": [investigation(counterparty_id="counterparty-b")]
    }
    with TestClient(create_app(openai_client=fake)) as client:
        response = client.post(
            "/investigate/conclude",
            json=conclude_request(idempotency_key="additional-key"),
        )

    assert response.status_code == 200
    assert response.json()["additionalInvestigations"][0]["params"] == {
        "counterpartyId": "counterparty-b",
        "dateFrom": None,
        "dateTo": None,
        "actionType": None,
    }


def test_invalid_structured_output_is_a_contract_rejection() -> None:
    fake = FakeOpenAI()
    fake.responses.plan_output = {
        "investigations": [
            {
                **investigation(),
                "reason": "거래처가 사흘 늦는지 확인합니다.",
            }
        ]
    }
    with TestClient(create_app(openai_client=fake)) as client:
        first = client.post("/investigate/plan", json=plan_request())
        second = client.post("/investigate/plan", json=plan_request())

    assert first.status_code == 422
    assert second.status_code == 422
    assert first.json()["detail"] == {"code": "invalid_investigation_response"}
    assert len(fake.responses.calls) == 2


def test_plan_and_conclude_idempotency_namespaces_are_independent() -> None:
    fake = FakeOpenAI()
    with TestClient(create_app(openai_client=fake)) as client:
        first_plan = client.post("/investigate/plan", json=plan_request())
        first_conclusion = client.post("/investigate/conclude", json=conclude_request())
        cached_plan = client.post("/investigate/plan", json=plan_request())
        cached_conclusion = client.post("/investigate/conclude", json=conclude_request())

    assert first_plan.status_code == 200
    assert first_conclusion.status_code == 200
    assert cached_plan.json() == first_plan.json()
    assert cached_conclusion.json() == first_conclusion.json()
    assert len(fake.responses.calls) == 2


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/investigate/plan", plan_request()),
        ("/investigate/conclude", conclude_request()),
    ],
)
def test_changed_payload_for_same_endpoint_idempotency_key_is_rejected(
    path: str,
    payload: dict[str, Any],
) -> None:
    fake = FakeOpenAI()
    changed = deepcopy(payload)
    changed["locale"] = "en-US"
    with TestClient(create_app(openai_client=fake)) as client:
        assert client.post(path, json=payload).status_code == 200
        conflict = client.post(path, json=changed)

    assert conflict.status_code == 409
    assert len(fake.responses.calls) == 1


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/investigate/plan", plan_request()),
        ("/investigate/conclude", conclude_request()),
    ],
)
def test_investigation_endpoints_require_openai_configuration(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    payload: dict[str, Any],
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(create_app(openai_client=None)) as client:
        response = client.post(path, json=payload)

    assert response.status_code == 503


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/investigate/plan", plan_request()),
        ("/investigate/conclude", conclude_request()),
    ],
)
def test_provider_failure_is_reported_as_502(
    path: str,
    payload: dict[str, Any],
) -> None:
    fake = FakeOpenAI()
    fake.responses.failure = RuntimeError("forced provider failure")
    with TestClient(create_app(openai_client=fake)) as client:
        response = client.post(path, json=payload)

    assert response.status_code == 502


@pytest.mark.parametrize(
    ("failure_kind", "expected_status", "expected_detail"),
    [
        ("rejected", 422, {"code": "invalid_investigation_response"}),
        ("failed", 502, "AI investigation plan failed"),
    ],
)
def test_concurrent_plan_follower_shares_the_owner_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    expected_status: int,
    expected_detail: Any,
) -> None:
    owner_started = ThreadEvent()
    owner_release = ThreadEvent()
    follower_waiting = ThreadEvent()

    class ObservedEvent:
        def __init__(self) -> None:
            self._event = ThreadEvent()

        def wait(self, timeout: float | None = None) -> bool:
            follower_waiting.set()
            return self._event.wait(timeout)

        def set(self) -> None:
            self._event.set()

    monkeypatch.setattr("flowguard.ai_service.Event", ObservedEvent)
    fake = FakeOpenAI()

    def parse_failure(**kwargs: Any) -> SimpleNamespace:
        fake.responses.calls.append(kwargs)
        owner_started.set()
        if not owner_release.wait(timeout=5):
            raise TimeoutError("test did not release the investigation owner")
        if failure_kind == "failed":
            raise RuntimeError("forced provider failure")
        return SimpleNamespace(
            output_parsed={
                "investigations": [
                    {
                        **investigation(),
                        "reason": "거래처가 사흘 늦는지 확인합니다.",
                    }
                ]
            }
        )

    fake.responses.parse = parse_failure  # type: ignore[method-assign]
    with (
        TestClient(create_app(openai_client=fake)) as client,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        owner_future = executor.submit(
            client.post,
            "/investigate/plan",
            json=plan_request(),
        )
        assert owner_started.wait(timeout=2)
        follower_future = executor.submit(
            client.post,
            "/investigate/plan",
            json=plan_request(),
        )
        try:
            assert follower_waiting.wait(timeout=2)
        finally:
            owner_release.set()
        owner = owner_future.result(timeout=5)
        follower = follower_future.result(timeout=5)

    assert owner.status_code == expected_status
    assert follower.status_code == expected_status
    assert owner.json()["detail"] == expected_detail
    assert follower.json() == owner.json()
    assert len(fake.responses.calls) == 1
