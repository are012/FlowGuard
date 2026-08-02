from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from flowguard.main import create_app
from flowguard.services.analysis import AnalysisOrchestrator
from flowguard.services.analysis_support import InterpretationOutcome
from flowguard.services.data import DataService
from flowguard.services.investigation_loop import AIInvestigationCallOutcome
from flowguard.storage import FlowGuardRepository, StorageError

USER_ID = "user-1"
AS_OF = datetime.fromisoformat("2026-07-31T09:00:00+09:00")
SAMPLE_CSV = (
    Path(__file__).parents[2]
    / "web"
    / "public"
    / "samples"
    / "flowguard-synthetic-transactions.csv"
)
NO_RISK_CSV = (
    "record_type,account_id,name,account_type,balance,is_payment_account,"
    "is_available_for_transfer,updated_at\n"
    "ACCOUNT,account-main,Main,CHECKING,1000000,true,true,"
    "2026-07-31T09:00:00+09:00\n"
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
DETERMINISTIC_TOOL_NAMES = [
    "get_financial_context",
    "get_counterparty_evidence",
    "get_counterparty_evidence",
    "evaluate_action_plan",
    "validate_financial_policy",
    "evaluate_action_plan",
    "validate_financial_policy",
]
ALL_ACTION_TYPES = [
    "transfer",
    "reserve_funds",
    "adjust_discretionary_budget",
    "pause_savings",
    "shift_payment_date",
    "delay_purchase",
    "add_installment",
    "confirm_receivable",
]


class InterpretationStub:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def interpret(self, payload: Mapping[str, Any]) -> InterpretationOutcome:
        self.requests.append(deepcopy(dict(payload)))
        return InterpretationOutcome(
            status="FALLBACK",
            response_payload={},
            attempt_count=0,
            latency_ms=0,
            error_code="test_interpretation_disabled",
        )


class MustNotCallInvestigationClient:
    @staticmethod
    def plan(
        _payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> AIInvestigationCallOutcome:
        raise AssertionError(f"investigation plan must stay disabled: {timeout_seconds}")

    @staticmethod
    def conclude(
        _payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> AIInvestigationCallOutcome:
        raise AssertionError(f"investigation conclusion must stay disabled: {timeout_seconds}")


ScriptedResult = Mapping[str, Any] | AIInvestigationCallOutcome


@dataclass
class ScriptedInvestigationClient:
    plan_result: ScriptedResult
    conclude_results: list[ScriptedResult]
    calls: list[tuple[str, dict[str, Any], float]] = field(default_factory=list)

    def plan(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> AIInvestigationCallOutcome:
        copied = deepcopy(dict(payload))
        self.calls.append(("plan", copied, timeout_seconds))
        return self._result(copied, self.plan_result)

    def conclude(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> AIInvestigationCallOutcome:
        copied = deepcopy(dict(payload))
        self.calls.append(("conclude", copied, timeout_seconds))
        return self._result(copied, self.conclude_results.pop(0))

    @staticmethod
    def _result(
        request: Mapping[str, Any],
        scripted: ScriptedResult,
    ) -> AIInvestigationCallOutcome:
        if isinstance(scripted, AIInvestigationCallOutcome):
            return scripted
        response = {field_name: request[field_name] for field_name in ECHO_FIELDS}
        response.update(deepcopy(dict(scripted)))
        return AIInvestigationCallOutcome(
            status="SUCCEEDED",
            response_payload=response,
            attempt_count=1,
            latency_ms=1,
        )


def _investigation(
    *,
    tool: str = "get_counterparty_evidence",
    params: dict[str, Any] | None = None,
    reason: str = "입금 예정 거래처의 지급 이력을 확인합니다.",
) -> dict[str, Any]:
    return {
        "tool": tool,
        "params": {"counterpartyId": "client-a"} if params is None else params,
        "reason": reason,
    }


def _conclusion() -> dict[str, Any]:
    return {
        "conclusion": {
            "hypotheses": [
                {
                    "type": "COUNTERPARTY_DELAY",
                    "summary": "거래처 지급 변동이 유동성 위험을 키울 수 있습니다.",
                    "priority": 1,
                }
            ],
            # These remain audit metadata and cannot reorder or select the
            # deterministic financial candidates.
            "candidatePriorities": ["adjust_discretionary_budget", "transfer"],
            "unresolved": [],
        }
    }


def _successful_client() -> ScriptedInvestigationClient:
    return ScriptedInvestigationClient(
        plan_result={"investigations": [_investigation()]},
        conclude_results=[_conclusion()],
    )


def _repository_with_sample() -> FlowGuardRepository:
    repository = FlowGuardRepository("sqlite:///:memory:")
    DataService(repository).import_csv(USER_ID, SAMPLE_CSV.read_bytes())
    return repository


def _run_sample(
    *,
    mode: str,
    investigation_client: Any,
    repository: FlowGuardRepository | None = None,
) -> tuple[
    FlowGuardRepository,
    AnalysisOrchestrator,
    dict[str, Any],
    dict[str, Any],
    InterpretationStub,
]:
    repository = repository or _repository_with_sample()
    interpretation_client = InterpretationStub()
    orchestrator = AnalysisOrchestrator(
        repository,
        ai_client=interpretation_client,  # type: ignore[arg-type]
        investigation_mode=mode,  # type: ignore[arg-type]
        investigation_client=investigation_client,
        investigation_model_name="test-model",
    )
    analysis = orchestrator.run(
        USER_ID,
        trigger_type="DATA_REFRESH",
        as_of=AS_OF,
    )
    report = repository.report_for_analysis(analysis["analysis_id"])
    return repository, orchestrator, analysis, report, interpretation_client


def _tool_names(executions: list[dict[str, Any]]) -> list[str]:
    return [str(item["tool_name"]) for item in executions]


def _candidate_signature(candidate: Mapping[str, Any]) -> dict[str, Any]:
    evaluation = candidate.get("evaluation") or {}
    policy = candidate.get("policy_result") or {}
    return {
        "type": candidate.get("type"),
        "amount": candidate.get("amount"),
        "feasible": candidate.get("feasible"),
        "risk_resolved": candidate.get("riskResolved"),
        "actions": candidate.get("actions"),
        "after_risk_metrics": (evaluation.get("after") or {}).get("risk_metrics"),
        "policy_valid": policy.get("valid"),
    }


def _financial_signature(report: Mapping[str, Any]) -> dict[str, Any]:
    safe_to_spend = dict(report["safe_to_spend"])
    safe_to_spend.pop("snapshot_id", None)
    agent = report["agent"]
    recommended = agent.get("recommended_plan")
    return {
        "risk_metrics": report["risk_metrics"],
        "cashflow": report["cashflow"],
        "safe_to_spend": safe_to_spend,
        "candidates": [_candidate_signature(candidate) for candidate in agent["actionCandidates"]],
        "recommended": (
            _candidate_signature(recommended) if isinstance(recommended, Mapping) else None
        ),
        "alternatives": (
            [_candidate_signature(candidate) for candidate in recommended.get("alternatives", [])]
            if isinstance(recommended, Mapping)
            else []
        ),
    }


def _numeric_date_signature(
    value: Any,
    path: tuple[str, ...] = (),
) -> tuple[tuple[tuple[str, ...], int | float | str], ...]:
    if isinstance(value, Mapping):
        return tuple(
            item
            for key in sorted(value, key=str)
            for item in _numeric_date_signature(value[key], (*path, str(key)))
        )
    if isinstance(value, (list, tuple)):
        return tuple(
            item
            for index, child in enumerate(value)
            for item in _numeric_date_signature(child, (*path, str(index)))
        )
    if isinstance(value, bool):
        return ()
    if isinstance(value, (int, float)):
        return ((path, value),)
    if isinstance(value, (date, datetime)):
        return ((path, value.isoformat()),)
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-" and value[7] == "-":
        try:
            date.fromisoformat(value[:10])
        except ValueError:
            return ()
        return ((path, value),)
    return ()


def test_off_mode_preserves_the_deterministic_analysis_and_interpret_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """off 로 끄면 조사 도입 이전과 완전히 동일한 계약을 유지해야 한다.

    프로덕션 기본값이 on 으로 바뀐 뒤에도 이 보장은 그대로다.
    조사를 끄는 것이 언제나 안전한 되돌리기 수단이어야 하기 때문이다.
    """

    monkeypatch.setenv("FLOWGUARD_AI_INVESTIGATION", "off")
    monkeypatch.setenv("FLOWGUARD_AI_CLASSIFICATION", "off")
    repository = FlowGuardRepository("sqlite:///:memory:")
    interpretation_client = InterpretationStub()
    app = create_app(
        repository,
        investigation_client=MustNotCallInvestigationClient(),
    )
    app.state.analysis_service.ai_client = interpretation_client
    app.state.data_service.import_csv(USER_ID, SAMPLE_CSV.read_bytes())

    analysis = app.state.analysis_service.run(
        USER_ID,
        trigger_type="DATA_REFRESH",
        as_of=AS_OF,
    )
    report = repository.report_for_analysis(analysis["analysis_id"])

    assert analysis["status"] == "COMPLETED"
    assert analysis["analysis_status"] == "SUCCEEDED"
    assert app.state.analysis_service.investigator.investigation_mode == "off"
    assert report["agent"]["decision_trace"]["mode"] == "DETERMINISTIC"
    assert report["versions"]["agent_mode"] == "DETERMINISTIC"
    assert report["versions"]["agent_prompt_version"] == "rule-based-investigator-1"
    assert "investigation" not in report["agent"]
    assert "investigation_trace" not in report["agent"]
    assert repository.list_investigation_runs(USER_ID, analysis_id=analysis["analysis_id"]) == []
    assert _tool_names(report["agent"]["tool_calls"]) == DETERMINISTIC_TOOL_NAMES
    assert [item["sequence"] for item in report["agent"]["tool_calls"]] == list(range(1, 8))

    assert len(interpretation_client.requests) == 1
    request = interpretation_client.requests[0]
    assert request["schemaVersion"] == "1.1"
    assert request["contractVersion"] == "1.1"
    assert request["promptVersion"] == "3"
    assert "facts" in request
    assert "evidence" in request
    assert "actionCandidates" in request


@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        ("FAILED", "connection_failed"),
        ("REJECTED", "invalid_investigation_response"),
    ],
)
def test_first_plan_failure_is_terminal_without_observations_and_falls_back(
    status: str,
    error_code: str,
) -> None:
    client = ScriptedInvestigationClient(
        plan_result=AIInvestigationCallOutcome(
            status=status,  # type: ignore[arg-type]
            response_payload={},
            attempt_count=1,
            latency_ms=1,
            error_code=error_code,
        ),
        conclude_results=[],
    )
    repository, _, analysis, report, _ = _run_sample(
        mode="on",
        investigation_client=client,
    )

    investigation = report["agent"]["investigation"]
    assert investigation["status"] == status
    assert investigation["applied"] is False
    assert investigation["observations"] == []
    assert investigation["tool_call_count"] == 0
    assert investigation["error_code"] == error_code
    assert report["agent"]["decision_trace"]["mode"] == "DETERMINISTIC"
    assert report["versions"]["agent_prompt_version"] == "rule-based-investigator-1"
    assert _tool_names(report["agent"]["tool_calls"]) == DETERMINISTIC_TOOL_NAMES
    assert _tool_names(repository.list_tool_executions(analysis["analysis_id"])) == (
        DETERMINISTIC_TOOL_NAMES
    )

    runs = repository.list_investigation_runs(USER_ID, analysis_id=analysis["analysis_id"])
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == status
    assert run["observations"] == []
    assert run["tool_call_count"] == 0
    turns = repository.list_investigation_turns(run["investigation_id"])
    assert len(turns) == 1
    assert turns[0]["status"] == status
    assert [call[0] for call in client.calls] == ["plan"]


def test_shadow_records_ai_after_deterministic_decisions_without_applying_it() -> None:
    _, _, _, off_report, _ = _run_sample(
        mode="off",
        investigation_client=MustNotCallInvestigationClient(),
    )
    client = _successful_client()
    repository, _, analysis, report, _ = _run_sample(
        mode="shadow",
        investigation_client=client,
    )

    assert _financial_signature(report) == _financial_signature(off_report)
    assert _tool_names(report["agent"]["tool_calls"]) == DETERMINISTIC_TOOL_NAMES
    assert [item["sequence"] for item in report["agent"]["tool_calls"]] == list(range(1, 8))
    assert report["agent"]["investigation"]["status"] == "SUCCEEDED"
    assert report["agent"]["investigation"]["applied"] is False
    assert report["agent"]["decision_trace"]["mode"] == "DETERMINISTIC"
    assert report["versions"]["agent_prompt_version"] == "rule-based-investigator-1"
    assert all(
        item["source"] == "baseline_result.risk_metrics"
        for item in report["agent"]["risk_hypotheses"]
    )

    audit = repository.list_tool_executions(analysis["analysis_id"])
    assert _tool_names(audit) == [*DETERMINISTIC_TOOL_NAMES, "get_counterparty_evidence"]
    assert [item["sequence"] for item in audit] == list(range(1, 9))
    runs = repository.list_investigation_runs(USER_ID, analysis_id=analysis["analysis_id"])
    assert len(runs) == 1
    assert runs[0]["mode"] == "SHADOW"
    assert runs[0]["status"] == "SUCCEEDED"
    assert runs[0]["tool_call_count"] == 1
    assert [call[0] for call in client.calls] == ["plan", "conclude"]


def test_on_success_uses_snapshot_targets_and_ai_observations_without_changing_numbers() -> None:
    _, _, _, off_report, _ = _run_sample(
        mode="off",
        investigation_client=MustNotCallInvestigationClient(),
    )
    client = _successful_client()
    repository, _, analysis, report, _ = _run_sample(
        mode="on",
        investigation_client=client,
    )

    assert _financial_signature(report) == _financial_signature(off_report)
    assert [candidate["type"] for candidate in report["agent"]["actionCandidates"]] == [
        "TRANSFER",
        "TRANSFER",
    ]
    assert all(
        "investigation_priority" not in candidate
        for candidate in report["agent"]["actionCandidates"]
    )
    assert report["agent"]["recommended_plan"]["type"] == "TRANSFER"
    assert report["agent"]["investigation"]["status"] == "SUCCEEDED"
    assert report["agent"]["investigation"]["applied"] is True
    assert report["agent"]["investigation"]["priorities"] == [
        "adjust_discretionary_budget",
        "transfer",
    ]
    assert report["agent"]["gathered_evidence"] == report["agent"]["investigation"]["observations"]
    assert all(item["source"] == "AI_INVESTIGATION" for item in report["agent"]["risk_hypotheses"])
    assert report["agent"]["decision_trace"]["mode"] == "AI_INVESTIGATED"
    assert report["versions"]["agent_model"] == "test-model"
    assert report["versions"]["agent_prompt_version"] == "invest-1"

    plan_request = client.calls[0][1]
    assert plan_request["snapshotId"] == report["snapshot_id"]
    assert plan_request["snapshotRevision"] == report["current_state_revision"]
    assert plan_request["targets"] == {
        "counterpartyIds": ["client-a", "client-b", "client-c"],
        "eventWindow": {"dateFrom": "2026-07-31", "dateTo": "2026-10-29"},
        "actionTypes": ALL_ACTION_TYPES,
    }
    assert plan_request["baseline"] == {
        "type": "PAYMENT_ACCOUNT",
        "date": "2026-08-25",
        "shortageAmount": 250_000,
    }

    public_tools = report["agent"]["tool_calls"]
    assert _tool_names(public_tools) == [
        "get_financial_context",
        "evaluate_action_plan",
        "validate_financial_policy",
        "evaluate_action_plan",
        "validate_financial_policy",
    ]
    assert "get_counterparty_evidence" not in _tool_names(public_tools)
    audit = repository.list_tool_executions(analysis["analysis_id"])
    assert _tool_names(audit) == ["get_counterparty_evidence", *_tool_names(public_tools)]
    assert [item["sequence"] for item in audit] == list(range(1, 7))

    runs = repository.list_investigation_runs(USER_ID, analysis_id=analysis["analysis_id"])
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "SUCCEEDED"
    assert run["mode"] == "ON"
    assert run["schema_version"] == "1.3"
    assert run["contract_version"] == "1.3"
    assert run["prompt_version"] == "invest-1"
    assert run["tool_call_count"] == 1
    assert run["priorities"] == ["adjust_discretionary_budget", "transfer"]
    assert len(run["observations"]) == 1
    assert set(run["observations"][0]) == {"tool", "params", "reason", "result"}
    assert set(run["observations"][0]["params"]) == {
        "counterpartyId",
        "dateFrom",
        "dateTo",
        "actionType",
    }
    turns = repository.list_investigation_turns(run["investigation_id"])
    assert [turn["turn_sequence"] for turn in turns] == [1, 2]
    assert [turn["phase"] for turn in turns] == [1, 2]
    assert [turn["endpoint"] for turn in turns] == [
        "/investigate/plan",
        "/investigate/conclude",
    ]
    assert all(turn["status"] == "SUCCEEDED" for turn in turns)


def test_complete_replay_reuses_the_stored_model_and_financial_result() -> None:
    client = _successful_client()
    _, orchestrator, analysis, report, _ = _run_sample(
        mode="on",
        investigation_client=client,
    )
    calls_before_replay = len(client.calls)
    orchestrator.investigator.investigation_model_name = "changed-runtime-model"

    replayed = orchestrator.investigator.investigate(
        analysis_id=analysis["analysis_id"],
        user_id=USER_ID,
        snapshot_id=report["snapshot_id"],
        snapshot_revision=report["current_state_revision"],
        baseline_result=report["agent"]["baseline_result"],
        investigation_targets=client.calls[0][1]["targets"],
    )

    assert len(client.calls) == calls_before_replay
    assert replayed["investigation"]["status"] == "SUCCEEDED"
    assert replayed["investigation"]["applied"] is True
    assert replayed["decision_mode"] == "AI_INVESTIGATED"
    assert replayed["decision_model"] == "test-model"
    assert replayed["risk"] == report["agent"]["risk"]
    assert [_candidate_signature(candidate) for candidate in replayed["actionCandidates"]] == [
        _candidate_signature(candidate) for candidate in report["agent"]["actionCandidates"]
    ]
    original_financial_values = {
        field: _numeric_date_signature(report["agent"][field], (field,))
        for field in ("risk", "actionCandidates", "recommended_plan")
    }
    replayed_financial_values = {
        field: _numeric_date_signature(replayed[field], (field,))
        for field in ("risk", "actionCandidates", "recommended_plan")
    }
    assert all(original_financial_values.values())
    assert replayed_financial_values == original_financial_values


def test_incomplete_succeeded_replay_audit_fails_closed_without_recalling_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository_with_sample()

    def existing_succeeded_run(**_kwargs: Any) -> tuple[dict[str, Any], bool]:
        return (
            {
                "investigation_id": "investigation-existing",
                "status": "SUCCEEDED",
                "additional_investigation_requested": False,
                "tool_call_count": 1,
                "observations": [
                    {
                        "tool": "get_counterparty_evidence",
                        "params": {"counterpartyId": "client-a"},
                        "reason": "저장된 거래처 지급 이력을 확인합니다.",
                        "result": {"counterparty_id": "client-a"},
                    }
                ],
                "hypotheses": [
                    {
                        "type": "STORED_AI_RESULT",
                        "summary": "불완전한 감사 기록의 결과입니다.",
                        "priority": 1,
                    }
                ],
                "priorities": ["transfer"],
                "unresolved": [],
                "total_latency_ms": 1,
                "error_code": None,
            },
            False,
        )

    def incomplete_turns(_investigation_id: str) -> list[dict[str, Any]]:
        return [
            {
                "turn_sequence": 1,
                "phase": 1,
                "endpoint": "/investigate/plan",
                "status": "SUCCEEDED",
                "response_payload": {"investigations": []},
            }
        ]

    monkeypatch.setattr(
        repository,
        "create_or_get_investigation_run",
        existing_succeeded_run,
    )
    monkeypatch.setattr(repository, "list_investigation_turns", incomplete_turns)
    _, _, analysis, report, _ = _run_sample(
        mode="on",
        investigation_client=MustNotCallInvestigationClient(),
        repository=repository,
    )

    investigation = report["agent"]["investigation"]
    assert analysis["status"] == "COMPLETED"
    assert investigation["investigation_id"] == "investigation-existing"
    assert investigation["status"] == "FAILED"
    assert investigation["applied"] is False
    assert investigation["error_code"] == "investigation_audit_incomplete"
    assert investigation["observations"] == []
    assert investigation["hypotheses"] == []
    assert report["agent"]["decision_trace"]["mode"] == "DETERMINISTIC"
    assert report["versions"]["agent_model"] is None
    assert report["versions"]["agent_prompt_version"] == "rule-based-investigator-1"
    assert all(
        item["source"] == "baseline_result.risk_metrics"
        for item in report["agent"]["risk_hypotheses"]
    )
    assert all("tool" not in item for item in report["agent"]["gathered_evidence"])
    assert _tool_names(report["agent"]["tool_calls"]) == DETERMINISTIC_TOOL_NAMES


@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        ("REJECTED", "invalid_investigation_response"),
        ("FAILED", "connection_failed"),
    ],
)
def test_conclude_failure_preserves_ai_audit_but_recollects_deterministic_evidence(
    status: str,
    error_code: str,
) -> None:
    _, _, _, off_report, _ = _run_sample(
        mode="off",
        investigation_client=MustNotCallInvestigationClient(),
    )
    client = ScriptedInvestigationClient(
        plan_result={"investigations": [_investigation()]},
        conclude_results=[
            AIInvestigationCallOutcome(
                status=status,  # type: ignore[arg-type]
                response_payload={},
                attempt_count=1,
                latency_ms=1,
                error_code=error_code,
            )
        ],
    )
    repository, _, analysis, report, _ = _run_sample(
        mode="on",
        investigation_client=client,
    )

    assert _financial_signature(report) == _financial_signature(off_report)
    investigation = report["agent"]["investigation"]
    assert investigation["status"] == "PARTIAL"
    assert investigation["applied"] is False
    assert investigation["error_code"] == error_code
    assert len(investigation["observations"]) == 1
    assert report["agent"]["decision_trace"]["mode"] == "AI_PARTIAL"
    assert report["versions"]["agent_prompt_version"] == "invest-1"
    assert all(
        item["source"] == "baseline_result.risk_metrics"
        for item in report["agent"]["risk_hypotheses"]
    )
    assert report["agent"]["gathered_evidence"] != investigation["observations"]
    assert all("tool" not in item for item in report["agent"]["gathered_evidence"])

    audit = repository.list_tool_executions(analysis["analysis_id"])
    assert _tool_names(audit) == ["get_counterparty_evidence", *DETERMINISTIC_TOOL_NAMES]
    assert _tool_names(report["agent"]["tool_calls"]) == DETERMINISTIC_TOOL_NAMES
    assert [item["sequence"] for item in report["agent"]["tool_calls"]] == list(range(2, 9))
    runs = repository.list_investigation_runs(USER_ID, analysis_id=analysis["analysis_id"])
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "PARTIAL"
    assert run["tool_call_count"] == 1
    assert len(run["observations"]) == 1
    turns = repository.list_investigation_turns(run["investigation_id"])
    assert [turn["status"] for turn in turns] == ["SUCCEEDED", status]


def test_five_ai_tools_and_seven_fallback_tools_share_sequence_not_budget() -> None:
    client = ScriptedInvestigationClient(
        plan_result={
            "investigations": [
                _investigation(
                    tool="get_financial_context",
                    params={},
                    reason="현재 자금 구성을 확인합니다.",
                ),
                _investigation(),
                _investigation(
                    tool="query_financial_events",
                    params={"dateFrom": "2026-07-31", "dateTo": "2026-10-29"},
                    reason="위험 구간의 금융 이벤트를 확인합니다.",
                ),
            ]
        },
        conclude_results=[
            {
                "additionalInvestigations": [
                    _investigation(
                        params={"counterpartyId": "client-b"},
                        reason="다른 입금 예정 거래처의 지급 이력을 확인합니다.",
                    ),
                    _investigation(
                        params={"counterpartyId": "client-c"},
                        reason="남은 입금 예정 거래처의 지급 이력을 확인합니다.",
                    ),
                ]
            },
            AIInvestigationCallOutcome(
                status="REJECTED",
                response_payload={},
                attempt_count=1,
                latency_ms=1,
                error_code="invalid_investigation_response",
            ),
        ],
    )

    repository, _, analysis, report, _ = _run_sample(
        mode="on",
        investigation_client=client,
    )

    ai_tool_names = [
        "get_financial_context",
        "get_counterparty_evidence",
        "query_financial_events",
        "get_counterparty_evidence",
        "get_counterparty_evidence",
    ]
    audit = repository.list_tool_executions(analysis["analysis_id"])
    assert _tool_names(audit) == [*ai_tool_names, *DETERMINISTIC_TOOL_NAMES]
    assert [item["sequence"] for item in audit] == list(range(1, 13))
    assert _tool_names(report["agent"]["tool_calls"]) == DETERMINISTIC_TOOL_NAMES
    assert [item["sequence"] for item in report["agent"]["tool_calls"]] == list(range(6, 13))
    assert report["agent"]["analysis_complete"] is True
    assert report["agent"]["investigation"]["status"] == "PARTIAL"
    assert report["agent"]["investigation"]["applied"] is False
    assert report["agent"]["decision_trace"]["mode"] == "AI_PARTIAL"

    runs = repository.list_investigation_runs(USER_ID, analysis_id=analysis["analysis_id"])
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "PARTIAL"
    assert run["tool_call_count"] == 5
    assert len(run["observations"]) == 5
    assert run["additional_investigation_requested"] is True
    turns = repository.list_investigation_turns(run["investigation_id"])
    assert [turn["turn_sequence"] for turn in turns] == [1, 2, 3]
    assert [turn["phase"] for turn in turns] == [1, 2, 2]
    assert [turn["status"] for turn in turns] == ["SUCCEEDED", "SUCCEEDED", "REJECTED"]
    assert [call[0] for call in client.calls] == ["plan", "conclude", "conclude"]


def test_turn_persistence_failure_fails_closed_and_uses_deterministic_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository_with_sample()

    def fail_turn_persistence(*_args: Any, **_kwargs: Any) -> None:
        raise StorageError("test turn persistence failure")

    monkeypatch.setattr(repository, "record_investigation_turn", fail_turn_persistence)
    repository, _, analysis, report, _ = _run_sample(
        mode="on",
        investigation_client=_successful_client(),
        repository=repository,
    )

    assert analysis["status"] == "COMPLETED"
    assert analysis["analysis_status"] == "SUCCEEDED"
    assert report["agent"]["investigation"]["status"] == "FAILED"
    assert report["agent"]["investigation"]["applied"] is False
    assert report["agent"]["investigation"]["error_code"] == ("investigation_persistence_error")
    assert report["agent"]["decision_trace"]["mode"] == "DETERMINISTIC"
    assert report["versions"]["agent_prompt_version"] == "rule-based-investigator-1"
    assert all(
        item["source"] == "baseline_result.risk_metrics"
        for item in report["agent"]["risk_hypotheses"]
    )
    assert all("tool" not in item for item in report["agent"]["gathered_evidence"])
    assert _tool_names(report["agent"]["tool_calls"]) == DETERMINISTIC_TOOL_NAMES

    runs = repository.list_investigation_runs(USER_ID, analysis_id=analysis["analysis_id"])
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "FAILED"
    assert run["error_code"] == "investigation_persistence_error"
    assert run["tool_call_count"] == 1
    assert len(run["observations"]) == 1


def test_on_mode_without_a_baseline_risk_never_calls_or_records_ai() -> None:
    repository = FlowGuardRepository("sqlite:///:memory:")
    DataService(repository).import_csv(USER_ID, NO_RISK_CSV)
    interpretation_client = InterpretationStub()
    orchestrator = AnalysisOrchestrator(
        repository,
        ai_client=interpretation_client,  # type: ignore[arg-type]
        investigation_mode="on",
        investigation_client=MustNotCallInvestigationClient(),
        investigation_model_name="test-model",
    )

    analysis = orchestrator.run(
        USER_ID,
        trigger_type="DATA_REFRESH",
        as_of=AS_OF,
    )
    report = repository.report_for_analysis(analysis["analysis_id"])

    assert analysis["status"] == "COMPLETED"
    assert report["risk_metrics"]["shortfall_type"] is None
    assert report["risk_metrics"]["expected_gap_max"] == 0
    assert report["agent"]["investigation"]["status"] == "NOT_REQUESTED"
    assert report["agent"]["investigation"]["applied"] is False
    assert report["agent"]["decision_trace"]["mode"] == "DETERMINISTIC"
    assert report["versions"]["agent_prompt_version"] == "rule-based-investigator-1"
    assert repository.list_investigation_runs(USER_ID, analysis_id=analysis["analysis_id"]) == []
    assert interpretation_client.requests == []


def test_virtual_analysis_never_starts_ai_investigation() -> None:
    client = _successful_client()
    repository, orchestrator, _, base_report, _ = _run_sample(
        mode="on",
        investigation_client=client,
    )
    recommendation = base_report["recommendations"][0]
    calls_before_virtual = len(client.calls)

    virtual_analysis = orchestrator.run_virtual(
        USER_ID,
        base_snapshot_id=recommendation["snapshot_id"],
        actions=recommendation["actions"],
        expected_current_state_revision=recommendation["current_state_revision"],
    )
    virtual_report = repository.report_for_analysis(virtual_analysis["analysis_id"])

    assert virtual_analysis["status"] == "COMPLETED"
    assert virtual_report["is_virtual"] is True
    assert len(client.calls) == calls_before_virtual
    assert (
        repository.list_investigation_runs(
            USER_ID,
            analysis_id=virtual_analysis["analysis_id"],
        )
        == []
    )
    assert "investigation" not in virtual_report["agent"]
    assert "investigation_trace" not in virtual_report["agent"]
    assert virtual_report["agent"]["decision_trace"]["mode"] == "DETERMINISTIC"
    assert virtual_report["versions"]["agent_mode"] == "DETERMINISTIC"
    assert virtual_report["versions"]["agent_prompt_version"] == "rule-based-investigator-1"
