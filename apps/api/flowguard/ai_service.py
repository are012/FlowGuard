"""Isolated AI interpretation service entrypoint.

Run this module as a separate process. It intentionally imports neither the
FlowGuard repository nor MCP tools; the backend sends only validated facts,
evidence, and action candidates through the versioned HTTP contract.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from typing import Annotated, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from flowguard.ai_contract import (
    AIActionCandidateRef,
    AIIdentifier,
    AIToBackendResponse,
    BackendToAIRequest,
    LabelClassificationGroup,
    LabelClassificationRequest,
    LabelClassificationResponse,
)
from flowguard.observability import configure_logging, log_event

load_dotenv(Path(__file__).resolve().parents[2] / "ai-service" / ".env", override=False)
logger = logging.getLogger("flowguard.ai_service")

INTERPRETATION_INSTRUCTIONS = """
You are FlowGuard's interpretation service. Rank and explain only the action
candidates supplied by the backend. Use only the supplied facts and evidence.
Never invent or change an amount, date, risk decision, or action ID. Omit any
candidate that is not feasible. Keep the user message concise (one or two
sentences) and write it in the requested locale. Do not expose hidden reasoning.
""".strip()

LABEL_CLASSIFICATION_INSTRUCTIONS = """
You are FlowGuard's label classification service. Group only labels that refer
to the same real-world entity, and never mix INFLOW and OUTFLOW labels. Use only
the supplied label ID, label text, direction, and occurrence count. Include
every supplied label ID exactly once, either in a group or in ungrouped, and
never invent an ID. Return only the requested structured fields. Do not put any
number, amount, date, account information, transaction ID, or probability in
normalizedName or reason. Do not expose hidden reasoning.
""".strip()


class InterpretationContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    riskExplanation: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]
    rankedActions: list[AIActionCandidateRef] = Field(default_factory=list)
    userMessage: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]

    @model_validator(mode="after")
    def rankings_are_unique(self) -> InterpretationContent:
        action_ids = [item.actionId for item in self.rankedActions]
        priorities = [item.priority for item in self.rankedActions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("ranked action IDs must be unique")
        if len(priorities) != len(set(priorities)):
            raise ValueError("ranked action priorities must be unique")
        return self


class LabelClassificationContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    groups: list[LabelClassificationGroup] = Field(max_length=500)
    ungrouped: list[AIIdentifier] = Field(max_length=500)

    @model_validator(mode="after")
    def group_ids_are_unique(self) -> LabelClassificationContent:
        group_ids = [item.groupId for item in self.groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("classification group IDs must be unique")
        return self


@dataclass(slots=True)
class _ClassificationInFlight:
    fingerprint: str
    completion: Event
    failure_status_code: int | None = None
    failure_detail: Any = None


def create_app(*, openai_client: Any | None = None, model: str | None = None) -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="FlowGuard AI Interpretation Service",
        version="1.2.0",
        description=(
            "Groups privacy-minimized labels, ranks validated FlowGuard actions, "
            "and generates user-facing explanations."
        ),
    )
    configured_model = model or os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
    client = openai_client
    if client is None and os.getenv("OPENAI_API_KEY", "").strip():
        from openai import OpenAI

        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=0)
    cache_lock = Lock()
    response_cache: dict[str, tuple[str, AIToBackendResponse]] = {}
    in_flight: dict[str, tuple[str, Event]] = {}
    classification_cache_lock = Lock()
    classification_response_cache: dict[str, tuple[str, LabelClassificationResponse]] = {}
    classification_in_flight: dict[str, _ClassificationInFlight] = {}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/interpret", response_model=AIToBackendResponse)
    def interpret(request: BackendToAIRequest) -> AIToBackendResponse:
        if client is None:
            raise HTTPException(status_code=503, detail="OpenAI is not configured")
        fingerprint = hashlib.sha256(
            json.dumps(
                request.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        with cache_lock:
            cached = response_cache.get(request.idempotencyKey)
            if cached is not None:
                if cached[0] != fingerprint:
                    raise HTTPException(status_code=409, detail="Idempotency key payload mismatch")
                log_event(
                    logger,
                    logging.INFO,
                    "ai_service_cache_hit",
                    analysis_id=request.analysisId,
                    ai_request_id=request.requestId,
                )
                return cached[1]
            pending = in_flight.get(request.idempotencyKey)
            if pending is None:
                completion = Event()
                in_flight[request.idempotencyKey] = (fingerprint, completion)
                owns_request = True
            else:
                if pending[0] != fingerprint:
                    raise HTTPException(status_code=409, detail="Idempotency key payload mismatch")
                completion = pending[1]
                owns_request = False
        if not owns_request:
            if not completion.wait(timeout=15):
                raise HTTPException(status_code=503, detail="Interpretation is still running")
            with cache_lock:
                cached = response_cache.get(request.idempotencyKey)
            if cached is None:
                raise HTTPException(status_code=503, detail="Interpretation did not complete")
            return cached[1]

        try:
            response = client.responses.parse(
                model=configured_model,
                instructions=INTERPRETATION_INSTRUCTIONS,
                input=json.dumps(
                    {
                        "locale": request.locale,
                        "facts": request.facts.model_dump(mode="json"),
                        "evidence": request.evidence,
                        "actionCandidates": [
                            item.model_dump(mode="json")
                            for item in request.actionCandidates
                            if item.feasible is True
                        ],
                    },
                    ensure_ascii=False,
                ),
                text_format=InterpretationContent,
                reasoning={"effort": "low", "context": "current_turn"},
                store=False,
            )
            content = response.output_parsed
            if content is None:
                raise ValueError("OpenAI response did not contain parsed output")
            content = InterpretationContent.model_validate(content)
        except Exception as exc:
            with cache_lock:
                in_flight.pop(request.idempotencyKey, None)
                completion.set()
            log_event(
                logger,
                logging.ERROR,
                "ai_service_interpretation_failed",
                analysis_id=request.analysisId,
                ai_request_id=request.requestId,
                exception_type=type(exc).__name__,
            )
            raise HTTPException(status_code=502, detail="AI interpretation failed") from exc

        candidate_ids = {item.actionId for item in request.actionCandidates if item.feasible}
        ranked_ids = {item.actionId for item in content.rankedActions}
        if not ranked_ids.issubset(candidate_ids):
            with cache_lock:
                in_flight.pop(request.idempotencyKey, None)
                completion.set()
            log_event(
                logger,
                logging.WARNING,
                "ai_service_interpretation_rejected",
                analysis_id=request.analysisId,
                ai_request_id=request.requestId,
                error_code="unapproved_action",
            )
            raise HTTPException(status_code=502, detail="AI returned an unapproved action")

        result = AIToBackendResponse(
            **request.model_dump(
                mode="json",
                include={
                    "schemaVersion",
                    "contractVersion",
                    "promptVersion",
                    "requestId",
                    "idempotencyKey",
                    "analysisId",
                    "snapshotId",
                    "snapshotRevision",
                    "locale",
                },
            ),
            **content.model_dump(mode="json"),
        )
        with cache_lock:
            if len(response_cache) >= 512:
                response_cache.pop(next(iter(response_cache)))
            response_cache[request.idempotencyKey] = (fingerprint, result)
            in_flight.pop(request.idempotencyKey, None)
            completion.set()
        log_event(
            logger,
            logging.INFO,
            "ai_service_interpretation_finished",
            analysis_id=request.analysisId,
            ai_request_id=request.requestId,
            ranked_action_count=len(result.rankedActions),
        )
        return result

    @app.post("/classify/labels", response_model=LabelClassificationResponse)
    def classify_labels(request: LabelClassificationRequest) -> LabelClassificationResponse:
        if client is None:
            raise HTTPException(status_code=503, detail="OpenAI is not configured")
        fingerprint = hashlib.sha256(
            json.dumps(
                request.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        with classification_cache_lock:
            cached = classification_response_cache.get(request.idempotencyKey)
            if cached is not None:
                if cached[0] != fingerprint:
                    raise HTTPException(status_code=409, detail="Idempotency key payload mismatch")
                log_event(
                    logger,
                    logging.INFO,
                    "ai_service_classification_cache_hit",
                    import_id=request.importId,
                    ai_request_id=request.requestId,
                )
                return cached[1]
            pending = classification_in_flight.get(request.idempotencyKey)
            if pending is None:
                pending = _ClassificationInFlight(
                    fingerprint=fingerprint,
                    completion=Event(),
                )
                classification_in_flight[request.idempotencyKey] = pending
                owns_request = True
            else:
                if pending.fingerprint != fingerprint:
                    raise HTTPException(status_code=409, detail="Idempotency key payload mismatch")
                owns_request = False
        if not owns_request:
            if not pending.completion.wait(timeout=15):
                raise HTTPException(status_code=503, detail="Classification is still running")
            with classification_cache_lock:
                cached = classification_response_cache.get(request.idempotencyKey)
                failure_status_code = pending.failure_status_code
                failure_detail = pending.failure_detail
            if cached is not None:
                return cached[1]
            if failure_status_code is not None:
                raise HTTPException(
                    status_code=failure_status_code,
                    detail=failure_detail,
                )
            raise HTTPException(status_code=503, detail="Classification did not complete")

        try:
            response = client.responses.parse(
                model=configured_model,
                instructions=LABEL_CLASSIFICATION_INSTRUCTIONS,
                input=json.dumps(
                    {
                        "locale": request.locale,
                        "labels": [item.model_dump(mode="json") for item in request.labels],
                    },
                    ensure_ascii=False,
                ),
                text_format=LabelClassificationContent,
                reasoning={"effort": "low", "context": "current_turn"},
                store=False,
            )
            content = response.output_parsed
            if content is None:
                raise ValueError("OpenAI response did not contain parsed output")
            content = LabelClassificationContent.model_validate(content)
            result = LabelClassificationResponse(
                **request.model_dump(
                    mode="json",
                    include={
                        "schemaVersion",
                        "contractVersion",
                        "promptVersion",
                        "requestId",
                        "idempotencyKey",
                        "importId",
                        "locale",
                    },
                ),
                **content.model_dump(mode="json"),
            )
        except (ValidationError, ValueError) as exc:
            with classification_cache_lock:
                pending.failure_status_code = 422
                pending.failure_detail = {"code": "invalid_classification_response"}
                classification_in_flight.pop(request.idempotencyKey, None)
                pending.completion.set()
            log_event(
                logger,
                logging.WARNING,
                "ai_service_classification_rejected",
                import_id=request.importId,
                ai_request_id=request.requestId,
                exception_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_classification_response"},
            ) from exc
        except Exception as exc:
            with classification_cache_lock:
                pending.failure_status_code = 502
                pending.failure_detail = "AI classification failed"
                classification_in_flight.pop(request.idempotencyKey, None)
                pending.completion.set()
            log_event(
                logger,
                logging.ERROR,
                "ai_service_classification_failed",
                import_id=request.importId,
                ai_request_id=request.requestId,
                exception_type=type(exc).__name__,
            )
            raise HTTPException(status_code=502, detail="AI classification failed") from exc

        with classification_cache_lock:
            if len(classification_response_cache) >= 512:
                classification_response_cache.pop(next(iter(classification_response_cache)))
            classification_response_cache[request.idempotencyKey] = (fingerprint, result)
            classification_in_flight.pop(request.idempotencyKey, None)
            pending.completion.set()
        log_event(
            logger,
            logging.INFO,
            "ai_service_classification_finished",
            import_id=request.importId,
            ai_request_id=request.requestId,
            group_count=len(result.groups),
        )
        return result

    return app


app = create_app()


__all__ = [
    "INTERPRETATION_INSTRUCTIONS",
    "LABEL_CLASSIFICATION_INSTRUCTIONS",
    "InterpretationContent",
    "LabelClassificationContent",
    "app",
    "create_app",
]
