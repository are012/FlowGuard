"""Isolated AI interpretation service entrypoint.

Run this module as a separate process. It intentionally imports neither the
FlowGuard repository nor MCP tools; the backend sends only validated facts,
evidence, and action candidates through the versioned HTTP contract.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from threading import Event, Lock
from typing import Annotated, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from flowguard.ai_contract import (
    AIActionCandidateRef,
    AIToBackendResponse,
    BackendToAIRequest,
)

load_dotenv(Path(__file__).resolve().parents[2] / "ai-service" / ".env", override=False)

INTERPRETATION_INSTRUCTIONS = """
You are FlowGuard's interpretation service. Rank and explain only the action
candidates supplied by the backend. Use only the supplied facts and evidence.
Never invent or change an amount, date, risk decision, or action ID. Omit any
candidate that is not feasible. Keep the user message concise (one or two
sentences) and write it in the requested locale. Do not expose hidden reasoning.
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


def create_app(*, openai_client: Any | None = None, model: str | None = None) -> FastAPI:
    app = FastAPI(
        title="FlowGuard AI Interpretation Service",
        version="1.1.0",
        description="Ranks validated FlowGuard actions and generates user-facing explanations.",
    )
    configured_model = model or os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
    client = openai_client
    if client is None and os.getenv("OPENAI_API_KEY", "").strip():
        from openai import OpenAI

        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=0)
    cache_lock = Lock()
    response_cache: dict[str, tuple[str, AIToBackendResponse]] = {}
    in_flight: dict[str, tuple[str, Event]] = {}

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
            raise HTTPException(status_code=502, detail="AI interpretation failed") from exc

        candidate_ids = {item.actionId for item in request.actionCandidates if item.feasible}
        ranked_ids = {item.actionId for item in content.rankedActions}
        if not ranked_ids.issubset(candidate_ids):
            with cache_lock:
                in_flight.pop(request.idempotencyKey, None)
                completion.set()
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
        return result

    return app


app = create_app()


__all__ = ["INTERPRETATION_INSTRUCTIONS", "InterpretationContent", "app", "create_app"]
