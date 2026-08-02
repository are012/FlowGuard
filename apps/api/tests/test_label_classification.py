from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event as ThreadEvent
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from flowguard.ai_contract import (
    LabelCategoryHint,
    LabelClassificationRequest,
    LabelClassificationResponse,
    LabelEntityKind,
)
from flowguard.ai_service import create_app
from flowguard.services.label_group_validation import (
    AILabelClassificationClient,
    validate_label_group_response,
)


def classification_request() -> dict[str, Any]:
    return {
        "schemaVersion": "1.2",
        "contractVersion": "1.2",
        "promptVersion": "classify-1",
        "requestId": "ai-classify-request-001",
        "idempotencyKey": "import-001:labels-abc:contract-1.2:classify-1:ko-KR",
        "importId": "import-001",
        "locale": "ko-KR",
        "labels": [
            {
                "labelId": "L1",
                "text": "디자인 컴퍼니",
                "direction": "INFLOW",
                "occurrences": 3,
            },
            {
                "labelId": "L2",
                "text": "디자인컴퍼니",
                "direction": "INFLOW",
                "occurrences": 2,
            },
            {
                "labelId": "L3",
                "text": "작업실 임대",
                "direction": "OUTFLOW",
                "occurrences": 2,
            },
        ],
    }


def classification_response(**changes: Any) -> dict[str, Any]:
    request = classification_request()
    response = {
        key: request[key]
        for key in (
            "schemaVersion",
            "contractVersion",
            "promptVersion",
            "requestId",
            "idempotencyKey",
            "importId",
            "locale",
        )
    }
    response.update(
        {
            "groups": [
                {
                    "groupId": "G1",
                    "labelIds": ["L1", "L2"],
                    "normalizedName": "디자인컴퍼니",
                    "entityKind": "CLIENT",
                    "categoryHint": "RECEIVABLE",
                    "essentialHint": None,
                    "confidence": "HIGH",
                    "reason": "법인 표기만 다른 동일 상호입니다.",
                }
            ],
            "ungrouped": ["L3"],
            **changes,
        }
    )
    return response


def classification_client(transport: httpx.MockTransport) -> AILabelClassificationClient:
    return AILabelClassificationClient(
        base_url="http://ai.test",
        connect_timeout_seconds=0.01,
        read_timeout_seconds=0.01,
        total_timeout_seconds=0.1,
        transport=transport,
    )


def test_classification_contract_has_closed_enums_and_no_financial_fields() -> None:
    assert {item.value for item in LabelEntityKind} == {
        "CLIENT",
        "MERCHANT",
        "PLATFORM",
        "CARD_PAYMENT",
        "OTHER",
    }
    assert {item.value for item in LabelCategoryHint} == {
        "RECEIVABLE",
        "CARD_BILL",
        "INSTALLMENT_PAYMENT",
        "RENT",
        "INSURANCE",
        "UTILITY",
        "LOAN_PAYMENT",
        "TAX",
        "SAVINGS",
        "DISCRETIONARY_EXPENSE",
        "OTHER_INFLOW",
        "OTHER_OUTFLOW",
    }
    request = LabelClassificationRequest.model_validate(classification_request())
    assert set(request.labels[0].model_dump()) == {
        "labelId",
        "text",
        "direction",
        "occurrences",
    }

    invalid = classification_request()
    invalid["labels"][0]["amount"] = 1
    invalid["labels"][0]["date"] = "2026-08-01"
    invalid["labels"][0]["accountId"] = "account-1"
    invalid["labels"][0]["transactionId"] = "transaction-1"
    with pytest.raises(ValidationError):
        LabelClassificationRequest.model_validate(invalid)

    for unsafe_text in (
        "카드 자동이체 08월",
        "정산 십만원",
        "이천이십육년 정산",
        "삼십칠만 정산",
        "서른만원 정산",
        "한 달 구독",
        "두 달 연체",
        "세시 결제",
    ):
        unsafe = classification_request()
        unsafe["labels"][0]["text"] = unsafe_text
        with pytest.raises(ValidationError):
            LabelClassificationRequest.model_validate(unsafe)


def test_unknown_label_id_is_rejected() -> None:
    response = classification_response()
    response["groups"][0]["labelIds"] = ["L1", "UNKNOWN"]

    error = validate_label_group_response(
        LabelClassificationRequest.model_validate(classification_request()),
        LabelClassificationResponse.model_validate(response),
    )

    assert error == "unknown_label_id"


def test_duplicate_label_id_is_rejected() -> None:
    response = classification_response(ungrouped=["L2", "L3"])

    error = validate_label_group_response(
        LabelClassificationRequest.model_validate(classification_request()),
        LabelClassificationResponse.model_validate(response),
    )

    assert error == "duplicate_label_id"


def test_missing_label_id_is_rejected() -> None:
    response = classification_response(ungrouped=[])

    error = validate_label_group_response(
        LabelClassificationRequest.model_validate(classification_request()),
        LabelClassificationResponse.model_validate(response),
    )

    assert error == "missing_label_id"


def test_mixed_direction_group_is_rejected() -> None:
    response = classification_response(ungrouped=["L2"])
    response["groups"][0]["labelIds"] = ["L1", "L3"]

    error = validate_label_group_response(
        LabelClassificationRequest.model_validate(classification_request()),
        LabelClassificationResponse.model_validate(response),
    )

    assert error == "mixed_direction_group"


def test_category_hint_must_match_group_direction() -> None:
    response = classification_response()
    response["groups"][0]["categoryHint"] = "RENT"

    error = validate_label_group_response(
        LabelClassificationRequest.model_validate(classification_request()),
        LabelClassificationResponse.model_validate(response),
    )

    assert error == "category_direction_mismatch"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("normalizedName", "디자인컴퍼니 2"),
        ("reason", "삼십만원 정산으로 보입니다."),
        ("normalizedName", "이천이십육년 정산"),
        ("reason", "삼십칠만 규모로 보입니다."),
        ("normalizedName", "서른만원 정산"),
        ("reason", "한 달 주기로 보입니다."),
        ("normalizedName", "August payment"),
        ("reason", "The bill is fifty dollars."),
        ("reason", "Risk is ninety percent."),
        ("normalizedName", "Payment next week"),
        ("reason", "내일 결제될 항목입니다."),
        ("normalizedName", "$fifty subscription"),
        ("reason", "The charge is fifty USD."),
        ("normalizedName", "Payment next quarter"),
        ("reason", "내년 결제로 보입니다."),
    ],
)
def test_numeric_free_text_is_rejected(field: str, value: str) -> None:
    response = classification_response()
    response["groups"][0][field] = value

    error = validate_label_group_response(
        LabelClassificationRequest.model_validate(classification_request()),
        LabelClassificationResponse.model_validate(response),
    )

    assert error == "unsupported_numeric_claim"


def test_english_modal_may_is_not_mistaken_for_a_calendar_month() -> None:
    response = classification_response()
    response["groups"][0]["reason"] = "The labels may refer to the same merchant."

    error = validate_label_group_response(
        LabelClassificationRequest.model_validate(classification_request()),
        LabelClassificationResponse.model_validate(response),
    )

    assert error is None


def test_contract_echo_mismatch_is_rejected() -> None:
    response = classification_response(requestId="different")

    error = validate_label_group_response(
        LabelClassificationRequest.model_validate(classification_request()),
        LabelClassificationResponse.model_validate(response),
    )

    assert error == "requestId_mismatch"


def test_invalid_enum_is_rejected_by_http_client() -> None:
    response = classification_response()
    response["groups"][0]["categoryHint"] = "PROJECT_FEE"

    outcome = classification_client(
        httpx.MockTransport(lambda _request: httpx.Response(200, json=response))
    ).classify(classification_request())

    assert outcome.status == "REJECTED"
    assert outcome.error_code == "invalid_response"


def test_ai_service_contract_rejection_is_not_reported_as_transport_failure() -> None:
    outcome = classification_client(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                422,
                json={"detail": {"code": "invalid_classification_response"}},
            )
        )
    ).classify(classification_request())

    assert outcome.status == "REJECTED"
    assert outcome.error_code == "invalid_classification_response"


def test_classification_client_returns_validated_success() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=classification_response())

    outcome = classification_client(httpx.MockTransport(handler)).classify(classification_request())

    assert outcome.status == "SUCCEEDED"
    assert outcome.error_code is None
    assert outcome.response_payload["groups"][0]["labelIds"] == ["L1", "L2"]
    assert seen[0].url.path == "/classify/labels"


def test_classification_client_distinguishes_transport_failure() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("unavailable", request=request)

    outcome = classification_client(httpx.MockTransport(handler)).classify(classification_request())

    assert outcome.status == "FAILED"
    assert outcome.error_code == "connection_failed"
    assert outcome.attempt_count == 2
    assert calls == 2


def test_classification_client_reports_http_failure_as_failed() -> None:
    outcome = classification_client(
        httpx.MockTransport(lambda _request: httpx.Response(400))
    ).classify(classification_request())

    assert outcome.status == "FAILED"
    assert outcome.error_code == "http_400"


def test_classification_client_reports_invalid_json_as_rejected() -> None:
    outcome = classification_client(
        httpx.MockTransport(lambda _request: httpx.Response(200, content=b"not-json"))
    ).classify(classification_request())

    assert outcome.status == "REJECTED"
    assert outcome.error_code == "invalid_json"


class FakeClassificationResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_parsed={
                "groups": classification_response()["groups"],
                "ungrouped": ["L3"],
            }
        )


class FakeClassificationOpenAI:
    def __init__(self) -> None:
        self.responses = FakeClassificationResponses()


def test_ai_service_classifies_only_supplied_label_projection() -> None:
    fake = FakeClassificationOpenAI()
    with TestClient(create_app(openai_client=fake, model="test-model")) as client:
        response = client.post("/classify/labels", json=classification_request())

    assert response.status_code == 200
    assert response.json()["contractVersion"] == "1.2"
    call = fake.responses.calls[0]
    assert call["store"] is False
    assert "tools" not in call
    sent = json.loads(call["input"])
    assert set(sent) == {"locale", "labels"}
    assert all(
        set(label) == {"labelId", "text", "direction", "occurrences"} for label in sent["labels"]
    )


def test_ai_service_classification_has_its_own_idempotency_cache() -> None:
    fake = FakeClassificationOpenAI()
    with TestClient(create_app(openai_client=fake)) as client:
        first = client.post("/classify/labels", json=classification_request())
        second = client.post("/classify/labels", json=classification_request())

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert len(fake.responses.calls) == 1


def test_ai_service_classification_rejects_idempotency_key_payload_change() -> None:
    fake = FakeClassificationOpenAI()
    changed = classification_request()
    changed["locale"] = "en-US"
    with TestClient(create_app(openai_client=fake)) as client:
        assert client.post("/classify/labels", json=classification_request()).status_code == 200
        conflict = client.post("/classify/labels", json=changed)

    assert conflict.status_code == 409
    assert len(fake.responses.calls) == 1


def test_ai_service_rejects_duplicate_group_ids_without_leaving_request_in_flight() -> None:
    fake = FakeClassificationOpenAI()
    duplicate = classification_response()["groups"][0]
    original_parse = fake.responses.parse

    def parse_with_duplicates(**kwargs: Any) -> SimpleNamespace:
        original_parse(**kwargs)
        return SimpleNamespace(
            output_parsed={"groups": [duplicate, duplicate], "ungrouped": ["L3"]}
        )

    fake.responses.parse = parse_with_duplicates  # type: ignore[method-assign]
    with TestClient(create_app(openai_client=fake)) as client:
        first = client.post("/classify/labels", json=classification_request())
        second = client.post("/classify/labels", json=classification_request())

    assert first.status_code == 422
    assert second.status_code == 422
    assert first.json()["detail"]["code"] == "invalid_classification_response"
    assert len(fake.responses.calls) == 2


def test_ai_service_rejects_oversized_content_without_leaving_request_in_flight() -> None:
    fake = FakeClassificationOpenAI()
    template = classification_response()["groups"][0]

    def parse_oversized(**kwargs: Any) -> SimpleNamespace:
        fake.responses.calls.append(kwargs)
        return SimpleNamespace(
            output_parsed={
                "groups": [
                    {**template, "groupId": f"oversized-group-{index}"} for index in range(501)
                ],
                "ungrouped": [],
            }
        )

    fake.responses.parse = parse_oversized  # type: ignore[method-assign]
    with TestClient(create_app(openai_client=fake)) as client:
        first = client.post("/classify/labels", json=classification_request())
        second = client.post("/classify/labels", json=classification_request())

    assert first.status_code == 422
    assert second.status_code == 422
    assert len(fake.responses.calls) == 2


@pytest.mark.parametrize(
    ("failure_kind", "expected_status", "expected_detail"),
    [
        ("rejected", 422, {"code": "invalid_classification_response"}),
        ("failed", 502, "AI classification failed"),
    ],
)
def test_ai_service_concurrent_follower_shares_terminal_classification_failure(
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
    fake = FakeClassificationOpenAI()
    duplicate = classification_response()["groups"][0]

    def parse_failure(**kwargs: Any) -> SimpleNamespace:
        fake.responses.calls.append(kwargs)
        owner_started.set()
        if not owner_release.wait(timeout=5):
            raise TimeoutError("test did not release the classification owner")
        if failure_kind == "failed":
            raise RuntimeError("forced classification failure")
        return SimpleNamespace(
            output_parsed={"groups": [duplicate, duplicate], "ungrouped": ["L3"]}
        )

    fake.responses.parse = parse_failure  # type: ignore[method-assign]
    with (
        TestClient(create_app(openai_client=fake)) as client,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        owner_future = executor.submit(
            client.post,
            "/classify/labels",
            json=classification_request(),
        )
        assert owner_started.wait(timeout=2)
        follower_future = executor.submit(
            client.post,
            "/classify/labels",
            json=classification_request(),
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
