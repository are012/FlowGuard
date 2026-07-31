"""Select the configured liquidity investigator without requiring an API key."""

from __future__ import annotations

import os
from typing import Any

from flowguard.storage import FlowGuardRepository

from .investigator import LiquidityInvestigator
from .luna import LunaLiquidityInvestigator
from .tools import CoreToolService

SUPPORTED_AGENT_MODES = {"auto", "luna", "deterministic"}
SUPPORTED_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}


def build_investigator(
    repository: FlowGuardRepository,
    tools: CoreToolService,
    *,
    client: Any | None = None,
) -> LiquidityInvestigator | LunaLiquidityInvestigator:
    mode = os.getenv("FLOWGUARD_AGENT_MODE", "auto").strip().lower()
    if mode not in SUPPORTED_AGENT_MODES:
        mode = "auto"
    model = os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna"
    effort = os.getenv("OPENAI_REASONING_EFFORT", "low").strip().lower()
    if effort not in SUPPORTED_REASONING_EFFORTS:
        effort = "low"
    api_key = os.getenv("OPENAI_API_KEY", "").strip()

    if mode == "deterministic":
        return LiquidityInvestigator(repository, tools)

    if client is None and api_key:
        from openai import OpenAI

        timeout = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30"))
        client = OpenAI(
            api_key=api_key,
            timeout=timeout,
            max_retries=0,
        )

    if client is not None:
        return LunaLiquidityInvestigator(
            repository,
            tools,
            client,
            model=model,
            reasoning_effort=effort,
        )

    return LiquidityInvestigator(
        repository,
        tools,
        agent_mode="DETERMINISTIC_FALLBACK",
        agent_model=model,
        fallback_reason="OPENAI_API_KEY_NOT_CONFIGURED",
    )


__all__ = ["build_investigator"]
