"""Validated AI investigation client and bounded two-phase controller."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

import httpx

from flowguard.ai_contract import (
    Investigation,
    InvestigationConcludeRequest,
    InvestigationConcludeResponse,
    InvestigationPlanRequest,
    InvestigationPlanResponse,
)

from .investigation_validation import (
    validate_investigation_conclude_response,
    validate_investigation_plan_response,
)
from .observation_projection import ObservationProjectionError, project_observation

MAX_PHASES = 2
MAX_TOOL_CALLS = 6
PHASE_TIMEOUT = 5.0
# 단계마다 PHASE_TIMEOUT 을 소진할 수 있으므로 총예산은 그 합보다 커야 한다.
# 이전 값 8.0 은 5.0 x 2 = 10.0 보다 작아, 1차가 제한시간을 다 쓰면
# 2차가 시작조차 못 하고 total_timeout 으로 끝났다.
# 여기에 도구 실행과 검증에 쓸 여유를 더한다.
TOOL_EXECUTION_ALLOWANCE = 2.0
TOTAL_BUDGET = PHASE_TIMEOUT * MAX_PHASES + TOOL_EXECUTION_ALLOWANCE

RETRYABLE_STATUS_CODES = {429, 502, 503, 504}

CallStatus = Literal["SUCCEEDED", "REJECTED", "FAILED"]
LoopStatus = Literal["SUCCEEDED", "PARTIAL", "REJECTED", "FAILED"]
InvestigationEndpoint = Literal["/investigate/plan", "/investigate/conclude"]


@dataclass(frozen=True)
class AIInvestigationCallOutcome:
    """Result of one validated backend-to-AI HTTP request."""

    status: CallStatus
    response_payload: dict[str, Any]
    attempt_count: int
    latency_ms: int
    error_code: str | None = None


class InvestigationAIClient(Protocol):
    """Narrow seam used by the loop and its deterministic tests."""

    def plan(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> AIInvestigationCallOutcome: ...

    def conclude(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> AIInvestigationCallOutcome: ...


@dataclass(frozen=True)
class InvestigationTurnRecord:
    """Audit-ready record of an AI turn; tool raw results are never included."""

    turn_sequence: int
    phase: int
    endpoint: InvestigationEndpoint
    request_payload: dict[str, Any]
    response_payload: dict[str, Any]
    status: CallStatus
    error_code: str | None
    attempt_count: int
    latency_ms: int


@dataclass(frozen=True)
class InvestigationLoopOutcome:
    """Bounded investigation result consumed by the analysis integration layer."""

    status: LoopStatus
    observations: tuple[dict[str, Any], ...]
    turns: tuple[InvestigationTurnRecord, ...]
    additional_investigation_requested: bool
    tool_call_count: int
    total_latency_ms: int
    hypotheses: tuple[dict[str, Any], ...] = ()
    priorities: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    error_code: str | None = None


class AIInvestigationClient:
    """Call the isolated investigation endpoints and reject invalid responses."""

    def __init__(
        self,
        *,
        base_url: str,
        connect_timeout_seconds: float = 3.0,
        read_timeout_seconds: float = 5.0,
        max_retries: int = 1,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.connect_timeout_seconds = connect_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self.max_retries = max_retries
        self.transport = transport
        self.clock = clock

    def plan(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float = PHASE_TIMEOUT,
    ) -> AIInvestigationCallOutcome:
        return self._call(
            payload,
            endpoint="/investigate/plan",
            request_type=InvestigationPlanRequest,
            response_type=InvestigationPlanResponse,
            validator=validate_investigation_plan_response,
            timeout_seconds=timeout_seconds,
        )

    def conclude(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float = PHASE_TIMEOUT,
    ) -> AIInvestigationCallOutcome:
        return self._call(
            payload,
            endpoint="/investigate/conclude",
            request_type=InvestigationConcludeRequest,
            response_type=InvestigationConcludeResponse,
            validator=validate_investigation_conclude_response,
            timeout_seconds=timeout_seconds,
        )

    def _call(
        self,
        payload: Mapping[str, Any],
        *,
        endpoint: InvestigationEndpoint,
        request_type: type[InvestigationPlanRequest] | type[InvestigationConcludeRequest],
        response_type: type[InvestigationPlanResponse] | type[InvestigationConcludeResponse],
        validator: Callable[[Any, Any], str | None],
        timeout_seconds: float,
    ) -> AIInvestigationCallOutcome:
        started_at = self.clock()
        try:
            request_body = request_type.model_validate(payload)
        except Exception:
            return self._failure("REJECTED", "invalid_payload", 0, started_at)

        deadline = started_at + max(0.0, timeout_seconds)
        attempt_count = 0
        with httpx.Client(transport=self.transport, trust_env=False) as client:
            while attempt_count <= self.max_retries:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return self._failure(
                        "FAILED",
                        "total_timeout",
                        attempt_count,
                        started_at,
                    )
                attempt_count += 1
                timeout = httpx.Timeout(
                    timeout=remaining,
                    connect=min(self.connect_timeout_seconds, remaining),
                    read=min(self.read_timeout_seconds, remaining),
                    write=min(self.connect_timeout_seconds, remaining),
                    pool=min(self.connect_timeout_seconds, remaining),
                )
                try:
                    response = client.post(
                        f"{self.base_url}{endpoint}",
                        json=request_body.model_dump(mode="json"),
                        timeout=timeout,
                    )
                except httpx.ConnectError:
                    if self.clock() >= deadline:
                        return self._failure(
                            "FAILED",
                            "total_timeout",
                            attempt_count,
                            started_at,
                        )
                    if attempt_count <= self.max_retries:
                        continue
                    return self._failure(
                        "FAILED",
                        "connection_failed",
                        attempt_count,
                        started_at,
                    )
                except httpx.TimeoutException:
                    if self.clock() >= deadline:
                        return self._failure(
                            "FAILED",
                            "total_timeout",
                            attempt_count,
                            started_at,
                        )
                    if attempt_count <= self.max_retries:
                        continue
                    return self._failure(
                        "FAILED",
                        "timeout",
                        attempt_count,
                        started_at,
                    )
                except httpx.HTTPError:
                    if self.clock() >= deadline:
                        return self._failure(
                            "FAILED",
                            "total_timeout",
                            attempt_count,
                            started_at,
                        )
                    return self._failure(
                        "FAILED",
                        "http_client_error",
                        attempt_count,
                        started_at,
                    )

                if self.clock() >= deadline:
                    return self._failure(
                        "FAILED",
                        "total_timeout",
                        attempt_count,
                        started_at,
                    )
                if response.status_code in RETRYABLE_STATUS_CODES:
                    if attempt_count <= self.max_retries:
                        continue
                    return self._failure(
                        "FAILED",
                        f"http_{response.status_code}",
                        attempt_count,
                        started_at,
                    )
                if response.status_code == 422 and self._is_contract_rejection(response):
                    return self._failure(
                        "REJECTED",
                        "invalid_investigation_response",
                        attempt_count,
                        started_at,
                    )
                if not 200 <= response.status_code < 300:
                    return self._failure(
                        "FAILED",
                        f"http_{response.status_code}",
                        attempt_count,
                        started_at,
                    )
                try:
                    response_payload = response.json()
                except ValueError:
                    return self._failure(
                        "REJECTED",
                        "invalid_json",
                        attempt_count,
                        started_at,
                    )
                try:
                    validated = response_type.model_validate(response_payload)
                except Exception:
                    return self._failure(
                        "REJECTED",
                        "invalid_response",
                        attempt_count,
                        started_at,
                    )
                error_code = validator(request_body, validated)
                if self.clock() >= deadline:
                    return self._failure(
                        "FAILED",
                        "total_timeout",
                        attempt_count,
                        started_at,
                    )
                if error_code is not None:
                    return self._failure(
                        "REJECTED",
                        error_code,
                        attempt_count,
                        started_at,
                        response_payload=validated.model_dump(mode="json"),
                    )
                return AIInvestigationCallOutcome(
                    status="SUCCEEDED",
                    response_payload=validated.model_dump(mode="json"),
                    attempt_count=attempt_count,
                    latency_ms=self._elapsed_ms(started_at),
                )

        return self._failure("FAILED", "retry_exhausted", attempt_count, started_at)

    def _failure(
        self,
        status: Literal["REJECTED", "FAILED"],
        error_code: str,
        attempt_count: int,
        started_at: float,
        *,
        response_payload: dict[str, Any] | None = None,
    ) -> AIInvestigationCallOutcome:
        return AIInvestigationCallOutcome(
            status=status,
            response_payload=response_payload or {},
            attempt_count=attempt_count,
            latency_ms=self._elapsed_ms(started_at),
            error_code=error_code,
        )

    def _elapsed_ms(self, started_at: float) -> int:
        return max(0, round((self.clock() - started_at) * 1000))

    @staticmethod
    def _is_contract_rejection(response: httpx.Response) -> bool:
        try:
            detail = response.json().get("detail")
        except (AttributeError, ValueError):
            return False
        return isinstance(detail, Mapping) and detail.get("code") == (
            "invalid_investigation_response"
        )


class InvestigationLoop:
    """Execute at most two investigation batches within one global deadline."""

    def __init__(
        self,
        client: InvestigationAIClient,
        execute_tool: Callable[[Investigation], dict[str, Any]],
        *,
        clock: Callable[[], float] = time.monotonic,
        observation_projector: Callable[[Any, Mapping[str, Any]], dict[str, Any]] = (
            project_observation
        ),
        total_budget: float | None = None,
    ) -> None:
        self.client = client
        self.execute_tool = execute_tool
        self.clock = clock
        self.observation_projector = observation_projector
        # 동기 실행에서는 요청이 그만큼 점유되므로 예산을 좁게 둔다.
        # 요청 밖에서 실행할 때는 호출자가 더 넓은 예산을 줄 수 있다.
        self.total_budget = TOTAL_BUDGET if total_budget is None else total_budget
        if self.total_budget < PHASE_TIMEOUT * MAX_PHASES:
            raise ValueError(
                "total_budget must cover every phase timeout: "
                f"{self.total_budget} < {PHASE_TIMEOUT * MAX_PHASES}"
            )

    def run(self, payload: Mapping[str, Any]) -> InvestigationLoopOutcome:
        started_at = self.clock()
        deadline = started_at + self.total_budget
        turns: list[InvestigationTurnRecord] = []
        observations: list[dict[str, Any]] = []
        tool_call_count = 0
        additional_requested = False
        completed_phases = 0

        def finish(
            status: LoopStatus,
            *,
            error_code: str | None = None,
            conclusion: InvestigationConcludeResponse | None = None,
        ) -> InvestigationLoopOutcome:
            hypotheses: tuple[dict[str, Any], ...] = ()
            priorities: tuple[str, ...] = ()
            unresolved: tuple[str, ...] = ()
            if conclusion is not None and conclusion.conclusion is not None:
                hypotheses = tuple(
                    item.model_dump(mode="json") for item in conclusion.conclusion.hypotheses
                )
                priorities = tuple(item.value for item in conclusion.conclusion.candidatePriorities)
                unresolved = tuple(conclusion.conclusion.unresolved)
            return InvestigationLoopOutcome(
                status=status,
                observations=tuple(observations),
                turns=tuple(turns),
                additional_investigation_requested=additional_requested,
                tool_call_count=tool_call_count,
                total_latency_ms=max(0, round((self.clock() - started_at) * 1000)),
                hypotheses=hypotheses,
                priorities=priorities,
                unresolved=unresolved,
                error_code=error_code,
            )

        try:
            plan_request = InvestigationPlanRequest.model_validate(payload)
        except Exception:
            return finish("REJECTED", error_code="invalid_payload")

        plan_payload = plan_request.model_dump(mode="json")
        remaining = deadline - self.clock()
        if remaining <= 0:
            return finish("FAILED", error_code="total_budget_exhausted")
        try:
            plan_call = self.client.plan(
                plan_payload,
                timeout_seconds=min(PHASE_TIMEOUT, remaining),
            )
        except Exception:
            plan_call = AIInvestigationCallOutcome(
                status="FAILED",
                response_payload={},
                attempt_count=0,
                latency_ms=0,
                error_code="unexpected_client_error",
            )
        plan_response, plan_error = self._validate_plan_call(plan_request, plan_call)
        turns.append(
            self._turn(
                turn_sequence=1,
                phase=1,
                endpoint="/investigate/plan",
                request_payload=plan_payload,
                call=plan_call,
                validation_error=plan_error,
            )
        )
        if self.clock() >= deadline:
            turns[-1] = replace(
                turns[-1],
                status="FAILED",
                error_code="total_budget_exhausted",
            )
            return finish("FAILED", error_code="total_budget_exhausted")
        if plan_call.status != "SUCCEEDED":
            return finish(plan_call.status, error_code=plan_call.error_code)
        if plan_error is not None or plan_response is None:
            return finish("REJECTED", error_code=plan_error or "invalid_response")
        if len(plan_response.investigations) > MAX_TOOL_CALLS:
            turns[-1] = replace(
                turns[-1],
                status="REJECTED",
                error_code="tool_call_limit_exceeded",
            )
            return finish("REJECTED", error_code="tool_call_limit_exceeded")

        batch_error, attempted = self._execute_batch(
            plan_response.investigations,
            observations=observations,
            tool_call_count=tool_call_count,
            deadline=deadline,
        )
        tool_call_count += attempted
        if batch_error is not None:
            return finish(
                "PARTIAL" if observations else "FAILED",
                error_code=batch_error,
            )
        completed_phases = 1

        first_conclude_request = self._conclude_request(
            plan_request,
            observations,
            sequence=1,
            allow_additional=True,
        )
        first_result = self._call_conclude(
            first_conclude_request,
            turn_sequence=2,
            phase=2,
            deadline=deadline,
            turns=turns,
        )
        if isinstance(first_result, str):
            return finish("PARTIAL", error_code=first_result)
        if first_result.conclusion is not None:
            return finish("SUCCEEDED", conclusion=first_result)

        additional_requested = True
        additional = first_result.additionalInvestigations or []
        if completed_phases >= MAX_PHASES:
            turns[-1] = replace(
                turns[-1],
                status="REJECTED",
                error_code="phase_limit_exceeded",
            )
            return finish("PARTIAL", error_code="phase_limit_exceeded")
        if tool_call_count + len(additional) > MAX_TOOL_CALLS:
            turns[-1] = replace(
                turns[-1],
                status="REJECTED",
                error_code="tool_call_limit_exceeded",
            )
            return finish("PARTIAL", error_code="tool_call_limit_exceeded")

        batch_error, attempted = self._execute_batch(
            additional,
            observations=observations,
            tool_call_count=tool_call_count,
            deadline=deadline,
        )
        tool_call_count += attempted
        if batch_error is not None:
            return finish("PARTIAL", error_code=batch_error)
        completed_phases += 1

        final_conclude_request = self._conclude_request(
            plan_request,
            observations,
            sequence=2,
            allow_additional=False,
        )
        final_result = self._call_conclude(
            final_conclude_request,
            turn_sequence=3,
            phase=2,
            deadline=deadline,
            turns=turns,
        )
        if isinstance(final_result, str):
            return finish("PARTIAL", error_code=final_result)
        if final_result.conclusion is None:
            turns[-1] = replace(
                turns[-1],
                status="REJECTED",
                error_code="phase_limit_exceeded",
            )
            return finish("PARTIAL", error_code="phase_limit_exceeded")
        return finish("SUCCEEDED", conclusion=final_result)

    def _execute_batch(
        self,
        investigations: list[Investigation],
        *,
        observations: list[dict[str, Any]],
        tool_call_count: int,
        deadline: float,
    ) -> tuple[str | None, int]:
        attempted = 0
        for investigation in investigations:
            if tool_call_count + attempted >= MAX_TOOL_CALLS:
                return "tool_call_limit_exceeded", attempted
            if self.clock() >= deadline:
                return "total_budget_exhausted", attempted
            attempted += 1
            try:
                raw_result = self.execute_tool(investigation)
            except Exception:
                return "tool_execution_failed", attempted
            try:
                projected = self.observation_projector(investigation.tool, raw_result)
            except ObservationProjectionError as exc:
                return exc.code, attempted
            except Exception:
                return "observation_projection_failed", attempted
            observation = {
                **investigation.model_dump(mode="json"),
                "result": projected,
            }
            observations.append(observation)
            if self.clock() >= deadline:
                return "total_budget_exhausted", attempted
        return None, attempted

    def _call_conclude(
        self,
        request: InvestigationConcludeRequest,
        *,
        turn_sequence: int,
        phase: int,
        deadline: float,
        turns: list[InvestigationTurnRecord],
    ) -> InvestigationConcludeResponse | str:
        payload = request.model_dump(mode="json")
        remaining = deadline - self.clock()
        if remaining <= 0:
            call = AIInvestigationCallOutcome(
                status="FAILED",
                response_payload={},
                attempt_count=0,
                latency_ms=0,
                error_code="total_budget_exhausted",
            )
            turns.append(
                self._turn(
                    turn_sequence=turn_sequence,
                    phase=phase,
                    endpoint="/investigate/conclude",
                    request_payload=payload,
                    call=call,
                )
            )
            return "total_budget_exhausted"
        try:
            call = self.client.conclude(
                payload,
                timeout_seconds=min(PHASE_TIMEOUT, remaining),
            )
        except Exception:
            call = AIInvestigationCallOutcome(
                status="FAILED",
                response_payload={},
                attempt_count=0,
                latency_ms=0,
                error_code="unexpected_client_error",
            )
        response, validation_error = self._validate_conclude_call(request, call)
        turns.append(
            self._turn(
                turn_sequence=turn_sequence,
                phase=phase,
                endpoint="/investigate/conclude",
                request_payload=payload,
                call=call,
                validation_error=validation_error,
            )
        )
        if self.clock() >= deadline:
            turns[-1] = replace(
                turns[-1],
                status="FAILED",
                error_code="total_budget_exhausted",
            )
            return "total_budget_exhausted"
        if call.status != "SUCCEEDED":
            return call.error_code or "investigation_call_failed"
        if validation_error is not None or response is None:
            return validation_error or "invalid_response"
        return response

    @staticmethod
    def _validate_plan_call(
        request: InvestigationPlanRequest,
        call: AIInvestigationCallOutcome,
    ) -> tuple[InvestigationPlanResponse | None, str | None]:
        if call.status != "SUCCEEDED":
            return None, None
        try:
            response = InvestigationPlanResponse.model_validate(call.response_payload)
        except Exception:
            return None, "invalid_response"
        return response, validate_investigation_plan_response(request, response)

    @staticmethod
    def _validate_conclude_call(
        request: InvestigationConcludeRequest,
        call: AIInvestigationCallOutcome,
    ) -> tuple[InvestigationConcludeResponse | None, str | None]:
        if call.status != "SUCCEEDED":
            return None, None
        try:
            response = InvestigationConcludeResponse.model_validate(call.response_payload)
        except Exception:
            return None, "invalid_response"
        return response, validate_investigation_conclude_response(request, response)

    @staticmethod
    def _turn(
        *,
        turn_sequence: int,
        phase: int,
        endpoint: InvestigationEndpoint,
        request_payload: dict[str, Any],
        call: AIInvestigationCallOutcome,
        validation_error: str | None = None,
    ) -> InvestigationTurnRecord:
        return InvestigationTurnRecord(
            turn_sequence=turn_sequence,
            phase=phase,
            endpoint=endpoint,
            request_payload=request_payload,
            response_payload=call.response_payload,
            status="REJECTED" if validation_error is not None else call.status,
            error_code=validation_error or call.error_code,
            attempt_count=call.attempt_count,
            latency_ms=call.latency_ms,
        )

    @staticmethod
    def _conclude_request(
        plan_request: InvestigationPlanRequest,
        observations: list[dict[str, Any]],
        *,
        sequence: int,
        allow_additional: bool,
    ) -> InvestigationConcludeRequest:
        payload = plan_request.model_dump(mode="json")
        identity_seed = (
            f"{plan_request.requestId}\0{plan_request.idempotencyKey}\0conclude\0{sequence}"
        )
        digest = hashlib.sha256(identity_seed.encode()).hexdigest()
        payload.update(
            {
                "requestId": f"ai-investigation-{digest[:32]}",
                "idempotencyKey": f"investigation-conclude-{sequence}-{digest}",
                "observations": observations,
                "allowAdditionalInvestigations": allow_additional,
            }
        )
        return InvestigationConcludeRequest.model_validate(payload)


__all__ = [
    "AIInvestigationCallOutcome",
    "AIInvestigationClient",
    "InvestigationAIClient",
    "InvestigationLoop",
    "InvestigationLoopOutcome",
    "InvestigationTurnRecord",
    "MAX_PHASES",
    "MAX_TOOL_CALLS",
    "PHASE_TIMEOUT",
    "TOTAL_BUDGET",
]
