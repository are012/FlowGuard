"""Small analysis support services that keep the orchestrator thin."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

import httpx

from flowguard.ai_contract import AIToBackendResponse, BackendToAIRequest

RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
CONTRACT_VERSION = "1.1"
ISO_DATE_PATTERN = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
KOREAN_DATE_PATTERN = re.compile(r"(?:(\d{4})년\s*)?(\d{1,2})월\s*(\d{1,2})\s*일")
DELIMITED_DATE_PATTERN = re.compile(
    r"(?<!\d)(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})\.?(?!\d)"
)
NUMBER_TOKEN = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
WON_UNIT_PATTERN = r"(?:억\s*원?|만\s*원|천\s*원|원)"
WON_AMOUNT_PATTERN = re.compile(rf"(?<![\d,제])({NUMBER_TOKEN})\s*({WON_UNIT_PATTERN})")
BARE_COMMA_AMOUNT_PATTERN = re.compile(r"(?<![\d,])(-?\d{1,3}(?:,\d{3})+)(?![\d,]|\.\d)")
DAY_COUNT_PATTERN = re.compile(rf"(?<![\d.,])({NUMBER_TOKEN})\s*(?:영업\s*)?일")
PERCENTAGE_PATTERN = re.compile(rf"(?<![\d.,])({NUMBER_TOKEN})\s*(?:%|％|퍼센트|프로)")
RAW_RATIO_PATTERN = re.compile(r"(?<![\d.])(0\.\d+|1\.0+)(?![\d.%％])")
RANGE_SEPARATOR = r"(?:[~～\-–—]|에서)"
AMOUNT_RANGE_PATTERN = re.compile(
    rf"(?<![\d,]){NUMBER_TOKEN}\s*{RANGE_SEPARATOR}\s*{NUMBER_TOKEN}\s*{WON_UNIT_PATTERN}"
)
DAY_RANGE_PATTERN = re.compile(
    rf"(?<![\d.,]){NUMBER_TOKEN}\s*{RANGE_SEPARATOR}\s*{NUMBER_TOKEN}\s*(?:영업\s*)?일"
)
PERCENTAGE_RANGE_PATTERN = re.compile(
    rf"(?<![\d.,]){NUMBER_TOKEN}\s*{RANGE_SEPARATOR}\s*{NUMBER_TOKEN}\s*(?:%|％|퍼센트|프로)"
)
PERCENTAGE_POINT_PATTERN = re.compile(
    rf"(?<![\d.,]){NUMBER_TOKEN}\s*(?:(?:%|％)\s*(?:[pP]|포인트)|(?:퍼센트|프로)\s*포인트)"
)
COUNT_PATTERN = re.compile(rf"(?<![\d,])({NUMBER_TOKEN})\s*(?:건|회|명|개)")
ORDINAL_PATTERN = re.compile(r"(?:제\s*\d+\s*(?:원인|이유|단계)|\d+\s*(?:순위|번째))")
UNRECOGNIZED_NUMBER_PATTERN = re.compile(NUMBER_TOKEN)
UNPARSED_KOREAN_AMOUNT_PATTERN = re.compile(rf"(?<![\d,]){NUMBER_TOKEN}\s*(?:억|만|천|백|십)")
KOREAN_NUMBER_CHAR = r"[영공일이삼사오육칠팔구십백천만억]"
KOREAN_NUMBER_WORD = rf"{KOREAN_NUMBER_CHAR}+"
KOREAN_WORD_AMOUNT_PATTERN = re.compile(
    rf"(?<![\d,])(?={KOREAN_NUMBER_CHAR}*[십백천만억]){KOREAN_NUMBER_WORD}\s*원"
)
KOREAN_WORD_PERCENTAGE_PATTERN = re.compile(
    rf"(?<![\d,])(?={KOREAN_NUMBER_CHAR}*[십백천만억]){KOREAN_NUMBER_WORD}\s*(?:퍼센트|프로)"
)
KOREAN_WORD_DAY_PATTERN = re.compile(
    rf"(?<![\d,])(?={KOREAN_NUMBER_CHAR}*[십백천]){KOREAN_NUMBER_WORD}\s*일"
)
KOREAN_NATIVE_DURATION_PATTERN = re.compile(
    r"(?:하루|이틀|사흘|나흘|닷새|엿새|이레|여드레|아흐레|열흘|열\s*(?:하루|이틀|사흘|나흘))"
)
ON_TIME_PERCENTAGE_CONTEXT_PATTERN = re.compile(
    r"(?:정시\s*(?:지급|입금)?률|제때\s*(?:지급|입금)?(?:된\s*)?비율|"
    r"on[- ]?time\s+(?:payment\s+)?rate)[^0-9]{0,16}$",
    re.IGNORECASE,
)
CONFIDENCE_PERCENTAGE_CONTEXT_PATTERN = re.compile(
    r"(?:(?:데이터\s*)?신뢰도|(?:data\s+)?confidence)[^0-9]{0,16}$",
    re.IGNORECASE,
)
RISK_PERCENTAGE_CONTEXT_PATTERN = re.compile(
    r"(?:위험\s*)?(?:확률|가능성)[^0-9]{0,16}$",
    re.IGNORECASE,
)
AVERAGE_DAY_CONTEXT_PATTERN = re.compile(r"(?:평균|average)[^0-9]{0,16}$", re.IGNORECASE)
MEDIAN_DAY_CONTEXT_PATTERN = re.compile(
    r"(?:중앙값|중간값|median)[^0-9]{0,16}$",
    re.IGNORECASE,
)
MAXIMUM_DAY_CONTEXT_PATTERN = re.compile(
    r"(?:최대|최장|maximum|max)[^0-9]{0,16}$",
    re.IGNORECASE,
)
EVIDENCE_DAY_FIELDS = {
    "average_delay_days",
    "median_delay_days",
    "maximum_delay_days",
}
EVIDENCE_RATIO_FIELDS = {
    "on_time_rate",
    "data_confidence",
}
EVIDENCE_COUNT_FIELDS = {"payment_history_count"}


@dataclass(frozen=True)
class InterpretationOutcome:
    status: Literal["SUCCEEDED", "FALLBACK", "FAILED"]
    response_payload: dict[str, Any]
    attempt_count: int
    latency_ms: int
    error_code: str | None = None

    @property
    def fallback_used(self) -> bool:
        return self.status == "FALLBACK"

    def public_payload(self) -> dict[str, Any]:
        return {
            **self.response_payload,
            "source": "AI" if self.status == "SUCCEEDED" else "DETERMINISTIC_FALLBACK",
            "fallbackReason": self.error_code,
        }


class AIInterpretationClient:
    """Call the isolated AI service, validate its contract, and fall back safely."""

    def __init__(
        self,
        *,
        base_url: str,
        connect_timeout_seconds: float = 3.0,
        read_timeout_seconds: float = 10.0,
        total_timeout_seconds: float = 15.0,
        max_retries: int = 1,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.connect_timeout_seconds = connect_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self.total_timeout_seconds = total_timeout_seconds
        self.max_retries = max_retries
        self.transport = transport

    def interpret(self, payload: Mapping[str, Any]) -> InterpretationOutcome:
        started_at = time.monotonic()
        try:
            request_body = BackendToAIRequest.model_validate(payload)
        except Exception:
            return self._fallback(payload, "invalid_payload", 0, started_at)

        attempt_count = 0
        deadline = started_at + self.total_timeout_seconds
        with httpx.Client(transport=self.transport, trust_env=False) as client:
            while attempt_count <= self.max_retries:
                attempt_count += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        "total_timeout",
                        attempt_count - 1,
                        started_at,
                    )
                timeout = httpx.Timeout(
                    timeout=remaining,
                    connect=min(self.connect_timeout_seconds, remaining),
                    read=min(self.read_timeout_seconds, remaining),
                    write=min(self.connect_timeout_seconds, remaining),
                    pool=min(self.connect_timeout_seconds, remaining),
                )
                try:
                    response = client.post(
                        f"{self.base_url}/interpret",
                        json=request_body.model_dump(mode="json"),
                        timeout=timeout,
                    )
                except httpx.ConnectError:
                    if attempt_count <= self.max_retries:
                        continue
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        "connection_failed",
                        attempt_count,
                        started_at,
                    )
                except httpx.TimeoutException:
                    if attempt_count <= self.max_retries:
                        continue
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        "timeout",
                        attempt_count,
                        started_at,
                    )
                except httpx.HTTPError:
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        "http_client_error",
                        attempt_count,
                        started_at,
                    )

                if response.status_code in RETRYABLE_STATUS_CODES:
                    if attempt_count <= self.max_retries:
                        continue
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        f"http_{response.status_code}",
                        attempt_count,
                        started_at,
                    )
                if not 200 <= response.status_code < 300:
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        f"http_{response.status_code}",
                        attempt_count,
                        started_at,
                    )

                try:
                    response_payload = response.json()
                except ValueError:
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        "invalid_json",
                        attempt_count,
                        started_at,
                    )
                try:
                    validated = AIToBackendResponse.model_validate(response_payload)
                except Exception:
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        "invalid_response",
                        attempt_count,
                        started_at,
                    )

                mismatch = self._contract_mismatch(request_body, validated)
                if mismatch is not None:
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        mismatch,
                        attempt_count,
                        started_at,
                    )
                candidate_ids = {
                    item.actionId for item in request_body.actionCandidates if item.feasible is True
                }
                ranked_ids = {item.actionId for item in validated.rankedActions}
                if not ranked_ids.issubset(candidate_ids):
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        "unknown_ranked_action",
                        attempt_count,
                        started_at,
                    )
                unsupported_claim = self._unsupported_numeric_claim(request_body, validated)
                if unsupported_claim is not None:
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        unsupported_claim,
                        attempt_count,
                        started_at,
                    )
                return InterpretationOutcome(
                    status="SUCCEEDED",
                    response_payload=validated.model_dump(mode="json"),
                    attempt_count=attempt_count,
                    latency_ms=self._elapsed_ms(started_at),
                )

        return self._fallback(
            request_body.model_dump(mode="json"),
            "retry_exhausted",
            attempt_count,
            started_at,
        )

    @staticmethod
    def _contract_mismatch(
        request_body: BackendToAIRequest,
        response_body: AIToBackendResponse,
    ) -> str | None:
        fields = (
            "contractVersion",
            "promptVersion",
            "requestId",
            "idempotencyKey",
            "analysisId",
            "snapshotId",
            "snapshotRevision",
            "locale",
        )
        for field in fields:
            if getattr(request_body, field) != getattr(response_body, field):
                return f"{field}_mismatch"
        return None

    @staticmethod
    def _unsupported_numeric_claim(
        request_body: BackendToAIRequest,
        response_body: AIToBackendResponse,
    ) -> str | None:
        """Reject amount, date, duration, and percentage claims absent from the request."""

        supplied_amounts: set[Decimal] = set()
        supplied_day_counts: dict[str, set[Decimal]] = {
            field: set() for field in EVIDENCE_DAY_FIELDS
        }
        supplied_percentages: dict[str, set[Decimal]] = {
            field: set() for field in EVIDENCE_RATIO_FIELDS
        }
        supplied_counts: set[Decimal] = set()
        supplied_dates = {
            request_body.facts.nextRisk.date.isoformat(),
            request_body.facts.cashflowSummary.lowestBalanceDate.isoformat(),
        }

        def as_decimal(value: Any) -> Decimal | None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            number = Decimal(str(value))
            return number if number.is_finite() else None

        def add_amount(value: Any, *, allow_magnitude: bool = False) -> None:
            number = as_decimal(value)
            if number is not None:
                supplied_amounts.add(number)
                if allow_magnitude and number < 0:
                    supplied_amounts.add(-number)

        def latest_context(
            prefix: str,
            patterns: Mapping[str, re.Pattern[str]],
        ) -> str | None:
            matches = [
                (match.start(), field)
                for field, pattern in patterns.items()
                if (match := pattern.search(prefix)) is not None
            ]
            return max(matches, default=(-1, None))[1]

        def allowed_percentage_values(prefix: str) -> set[Decimal] | None:
            context = latest_context(
                prefix,
                {
                    "on_time_rate": ON_TIME_PERCENTAGE_CONTEXT_PATTERN,
                    "data_confidence": CONFIDENCE_PERCENTAGE_CONTEXT_PATTERN,
                    "risk_probability": RISK_PERCENTAGE_CONTEXT_PATTERN,
                },
            )
            return supplied_percentages.get(context) if context is not None else None

        def allowed_day_values(prefix: str) -> set[Decimal] | None:
            context = latest_context(
                prefix,
                {
                    "average_delay_days": AVERAGE_DAY_CONTEXT_PATTERN,
                    "median_delay_days": MEDIAN_DAY_CONTEXT_PATTERN,
                    "maximum_delay_days": MAXIMUM_DAY_CONTEXT_PATTERN,
                },
            )
            return supplied_day_counts.get(context) if context is not None else None

        def collect_evidence(value: Any, field_name: str | None = None) -> None:
            if isinstance(value, str):
                if field_name is not None and (
                    field_name == "date"
                    or field_name.endswith("_date")
                    or field_name.endswith("Date")
                ):
                    supplied_dates.update(ISO_DATE_PATTERN.findall(value))
                return
            if isinstance(value, Mapping):
                for field, item in value.items():
                    number = as_decimal(item)
                    if number is not None and field in EVIDENCE_DAY_FIELDS:
                        supplied_day_counts[field].add(number)
                    if number is not None and field in EVIDENCE_RATIO_FIELDS:
                        supplied_percentages[field].add(
                            number * 100 if Decimal(0) <= number <= Decimal(1) else number
                        )
                    if number is not None and field in EVIDENCE_COUNT_FIELDS:
                        supplied_counts.add(number)
                    collect_evidence(item, str(field))
                return
            if isinstance(value, list):
                for item in value:
                    collect_evidence(item, field_name)

        add_amount(request_body.facts.safeToSpend)
        add_amount(request_body.facts.nextRisk.shortageAmount)
        add_amount(
            request_body.facts.cashflowSummary.lowestBalance,
            allow_magnitude=True,
        )
        for candidate in request_body.actionCandidates:
            if candidate.feasible is True:
                add_amount(candidate.amount)
        collect_evidence(request_body.evidence)
        supplied_month_days = {
            (int(date_text[5:7]), int(date_text[8:10])) for date_text in supplied_dates
        }
        generated_text = "\n".join(
            [
                response_body.riskExplanation,
                response_body.userMessage,
                *(item.reason for item in response_body.rankedActions),
            ]
        )
        for year, month, day in DELIMITED_DATE_PATTERN.findall(generated_text):
            normalized_date = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
            if normalized_date not in supplied_dates:
                return "unknown_date_claim"
        for year, month, day in KOREAN_DATE_PATTERN.findall(generated_text):
            if (int(month), int(day)) not in supplied_month_days:
                return "unknown_date_claim"
            if year and not any(
                date_text.startswith(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")
                for date_text in supplied_dates
            ):
                return "unknown_date_claim"
        numeric_text = DELIMITED_DATE_PATTERN.sub(" ", generated_text)
        numeric_text = KOREAN_DATE_PATTERN.sub(" ", numeric_text)
        if KOREAN_WORD_AMOUNT_PATTERN.search(numeric_text):
            return "unknown_amount_claim"
        if KOREAN_WORD_PERCENTAGE_PATTERN.search(numeric_text):
            return "unknown_percentage_claim"
        if KOREAN_WORD_DAY_PATTERN.search(numeric_text) or KOREAN_NATIVE_DURATION_PATTERN.search(
            numeric_text
        ):
            return "unknown_duration_claim"
        if AMOUNT_RANGE_PATTERN.search(numeric_text):
            return "unknown_amount_claim"
        if DAY_RANGE_PATTERN.search(numeric_text):
            return "unknown_duration_claim"
        if PERCENTAGE_RANGE_PATTERN.search(numeric_text):
            return "unknown_percentage_claim"
        if PERCENTAGE_POINT_PATTERN.search(numeric_text):
            return "unknown_percentage_claim"
        multipliers = {
            "원": Decimal(1),
            "천원": Decimal(1_000),
            "만원": Decimal(10_000),
            "억": Decimal(100_000_000),
            "억원": Decimal(100_000_000),
        }
        for raw_amount, raw_unit in WON_AMOUNT_PATTERN.findall(numeric_text):
            unit = raw_unit.replace(" ", "")
            amount = Decimal(raw_amount.replace(",", "")) * multipliers[unit]
            if amount not in supplied_amounts:
                return "unknown_amount_claim"
        for percentage_match in PERCENTAGE_PATTERN.finditer(numeric_text):
            percentage = Decimal(percentage_match.group(1).replace(",", ""))
            prefix = numeric_text[max(0, percentage_match.start() - 64) : percentage_match.start()]
            allowed_percentages = allowed_percentage_values(prefix)
            if allowed_percentages is None:
                return "unknown_percentage_claim"
            if percentage not in allowed_percentages:
                return "unknown_percentage_claim"
        percentage_text = PERCENTAGE_PATTERN.sub(" ", numeric_text)
        for ratio_match in RAW_RATIO_PATTERN.finditer(percentage_text):
            ratio = Decimal(ratio_match.group(1))
            prefix = percentage_text[max(0, ratio_match.start() - 64) : ratio_match.start()]
            allowed_percentages = allowed_percentage_values(prefix)
            if allowed_percentages is None or ratio * 100 not in allowed_percentages:
                return "unknown_percentage_claim"
        for day_match in DAY_COUNT_PATTERN.finditer(numeric_text):
            day_count = Decimal(day_match.group(1).replace(",", ""))
            prefix = numeric_text[max(0, day_match.start() - 64) : day_match.start()]
            allowed_day_counts = allowed_day_values(prefix)
            if allowed_day_counts is None:
                return "unknown_duration_claim"
            if day_count not in allowed_day_counts:
                return "unknown_duration_claim"
        bare_number_text = WON_AMOUNT_PATTERN.sub(" ", numeric_text)
        if UNPARSED_KOREAN_AMOUNT_PATTERN.search(bare_number_text):
            return "unknown_amount_claim"
        bare_number_text = PERCENTAGE_PATTERN.sub(" ", bare_number_text)
        bare_number_text = RAW_RATIO_PATTERN.sub(" ", bare_number_text)
        bare_number_text = DAY_COUNT_PATTERN.sub(" ", bare_number_text)
        for count_match in COUNT_PATTERN.finditer(bare_number_text):
            count = Decimal(count_match.group(1).replace(",", ""))
            if count not in supplied_counts:
                return "unknown_numeric_claim"
        bare_number_text = COUNT_PATTERN.sub(" ", bare_number_text)
        for raw_amount in BARE_COMMA_AMOUNT_PATTERN.findall(bare_number_text):
            amount = Decimal(raw_amount.replace(",", ""))
            if amount not in supplied_amounts:
                return "unknown_amount_claim"
        bare_number_text = BARE_COMMA_AMOUNT_PATTERN.sub(" ", bare_number_text)
        bare_number_text = ORDINAL_PATTERN.sub(" ", bare_number_text)
        if UNRECOGNIZED_NUMBER_PATTERN.search(bare_number_text):
            return "unknown_numeric_claim"
        return None

    def _fallback(
        self,
        payload: Mapping[str, Any],
        error_code: str,
        attempt_count: int,
        started_at: float,
    ) -> InterpretationOutcome:
        envelope = {
            field: payload.get(field)
            for field in (
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
            if payload.get(field) is not None
        }
        return InterpretationOutcome(
            status="FALLBACK",
            response_payload={
                **envelope,
                "riskExplanation": "규칙 기반으로 위험을 요약했습니다.",
                "rankedActions": [],
                "userMessage": "금융 위험을 규칙 기반으로 확인했습니다.",
            },
            attempt_count=attempt_count,
            latency_ms=self._elapsed_ms(started_at),
            error_code=error_code,
        )

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return max(0, round((time.monotonic() - started_at) * 1000))


__all__ = [
    "AIInterpretationClient",
    "CONTRACT_VERSION",
    "InterpretationOutcome",
    "RETRYABLE_STATUS_CODES",
]
