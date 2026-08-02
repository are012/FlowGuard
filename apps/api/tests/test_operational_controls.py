from __future__ import annotations

import json
import logging
import subprocess
import sys
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flowguard.main import create_app
from flowguard.rate_limit import InMemoryRateLimiter, RateLimitRule
from flowguard.services.investigator import LiquidityInvestigator
from flowguard.storage import FlowGuardRepository


def _events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for record in caplog.records:
        if not record.name.startswith("flowguard"):
            continue
        try:
            events.append(json.loads(record.getMessage()))
        except json.JSONDecodeError:
            continue
    return events


def test_request_logs_are_structured_and_echo_a_safe_correlation_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = FlowGuardRepository("sqlite:///:memory:")
    caplog.set_level(logging.INFO, logger="flowguard")
    with TestClient(create_app(repository)) as client:
        response = client.get("/health", headers={"X-Request-ID": "walkthrough-1"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "walkthrough-1"
    completed = [item for item in _events(caplog) if item["event"] == "request_completed"]
    assert completed[-1] == {
        "duration_ms": completed[-1]["duration_ms"],
        "event": "request_completed",
        "method": "GET",
        "path": "/health",
        "request_id": "walkthrough-1",
        "status_code": 200,
        "timestamp": completed[-1]["timestamp"],
    }


def test_invalid_correlation_id_is_replaced() -> None:
    repository = FlowGuardRepository("sqlite:///:memory:")
    with TestClient(create_app(repository)) as client:
        response = client.get("/health", headers={"X-Request-ID": "unsafe request id"})

    assert response.headers["X-Request-ID"].startswith("request-")


def test_default_logging_emits_info_events_without_a_host_handler() -> None:
    script = """
import json
import logging
from flowguard.observability import configure_logging, log_event

logging.getLogger().handlers.clear()
application_logger = logging.getLogger("flowguard")
application_logger.handlers.clear()
application_logger.propagate = True
configure_logging()
log_event(logging.getLogger("flowguard.smoke"), logging.INFO, "smoke_ready", ready=True)
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    event = json.loads(result.stderr.strip())
    assert event["event"] == "smoke_ready"
    assert event["ready"] is True


def test_analysis_rate_limit_is_user_scoped_and_resets_without_sleeping() -> None:
    now = [100.0]
    limiter = InMemoryRateLimiter(
        [RateLimitRule("POST", "/api/v1/analyses", 1, 60)],
        clock=lambda: now[0],
    )
    repository = FlowGuardRepository("sqlite:///:memory:")
    with TestClient(create_app(repository, rate_limiter=limiter)) as client:
        first = client.post(
            "/api/v1/analyses",
            headers={"X-User-ID": "user-1"},
            json={"as_of": "2026-07-24T09:00:00+09:00"},
        )
        limited = client.post(
            "/api/v1/analyses",
            headers={"X-User-ID": "user-1"},
            json={"as_of": "2026-07-24T09:00:00+09:00"},
        )
        independent = client.post(
            "/api/v1/analyses",
            headers={"X-User-ID": "user-2"},
            json={"as_of": "2026-07-24T09:00:00+09:00"},
        )
        now[0] += 61
        reset = client.post(
            "/api/v1/analyses",
            headers={"X-User-ID": "user-1"},
            json={"as_of": "2026-07-24T09:00:00+09:00"},
        )

    assert first.status_code == 201
    assert independent.status_code == 201
    assert reset.status_code == 201
    assert limited.status_code == 429
    assert limited.json() == {
        "code": "RATE_LIMITED",
        "message": "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
        "details": {"retry_after_seconds": 60},
        "retryable": True,
    }
    assert limited.headers["Retry-After"] == "60"
    assert limited.headers["RateLimit-Remaining"] == "0"


def test_upload_rate_limit_rejects_before_a_second_import() -> None:
    limiter = InMemoryRateLimiter([RateLimitRule("POST", "/api/v1/imports/transactions", 1, 60)])
    repository = FlowGuardRepository("sqlite:///:memory:")
    csv_body = "record_type,account_id,name,account_type,balance\nACCOUNT,a,A,CHECKING,1000\n"
    with TestClient(create_app(repository, rate_limiter=limiter)) as client:
        first = client.post(
            "/api/v1/imports/transactions",
            files={"file": ("first.csv", csv_body, "text/csv")},
        )
        second = client.post(
            "/api/v1/imports/transactions",
            files={"file": ("second.csv", csv_body.replace("ACCOUNT,a", "ACCOUNT,b"), "text/csv")},
        )

    assert first.status_code == 201
    assert second.status_code == 429
    assert [item["account_id"] for item in repository.list_records("demo-user", "accounts")] == [
        "a"
    ]


def test_analysis_and_policy_outcomes_emit_semantic_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class RiskBuilder:
        @staticmethod
        def build(
            _snapshot_id: str,
            _baseline: dict[str, Any],
            _call: Any,
        ) -> dict[str, Any]:
            return {
                "risk": {"type": "TOTAL_LIQUIDITY"},
                "gathered_evidence": [],
                "evidence_gaps": [],
                "context": {},
            }

    class ActionService:
        @staticmethod
        def create(**_kwargs: Any) -> list[dict[str, Any]]:
            return [{"action_id": "candidate-1", "actions": []}]

        @staticmethod
        def evaluate(
            _snapshot_id: str,
            _candidates: list[dict[str, Any]],
            _call: Any,
        ) -> list[dict[str, Any]]:
            return [
                {
                    "action_id": "candidate-1",
                    "actions": [],
                    "evaluation": {"valid": True},
                    "policy_result": {
                        "valid": False,
                        "violations": [{"code": "PROTECTED_FUNDS"}],
                    },
                    "feasible": False,
                    "riskResolved": False,
                }
            ]

    caplog.set_level(logging.INFO, logger="flowguard")
    repository = FlowGuardRepository("sqlite:///:memory:")
    with TestClient(create_app(repository)) as client:
        response = client.post(
            "/api/v1/analyses",
            json={"as_of": "2026-07-24T09:00:00+09:00"},
        )
    assert response.status_code == 201
    events = _events(caplog)
    assert any(item["event"] == "analysis_started" for item in events)
    assert any(
        item["event"] == "analysis_finished" and item["analysis_status"] == "FAILED"
        for item in events
    )
    assert any(item["event"] == "analysis_failed" for item in events)

    analysis = repository.create_analysis("user-1", trigger_type="MANUAL")
    investigator = LiquidityInvestigator(repository, object())  # type: ignore[arg-type]
    investigator.risk_builder = RiskBuilder()  # type: ignore[assignment]
    investigator.action_service = ActionService()  # type: ignore[assignment]

    investigator.investigate(
        analysis_id=analysis["analysis_id"],
        snapshot_id="snapshot-1",
        baseline_result={
            "risk_metrics": {
                "shortfall_type": "TOTAL_LIQUIDITY",
                "first_risk_date": "2026-07-25",
                "expected_gap_max": 10_000,
            }
        },
    )

    rejected = [item for item in _events(caplog) if item["event"] == "policy_candidate_rejected"]
    assert rejected[-1]["analysis_id"] == analysis["analysis_id"]
    assert rejected[-1]["violation_codes"] == "PROTECTED_FUNDS"
