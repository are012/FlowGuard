"""Read models for the feature-oriented FlowGuard screens."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flowguard.storage import FlowGuardRepository, RecordNotFound

from .errors import ServiceError
from .tools import CoreToolService

_STATUS_SEVERITY = {
    "STABLE": 0,
    "VERIFY": 1,
    "PREPARE": 2,
    "ACT_NOW": 3,
}


class ReportQueryService:
    def __init__(self, repository: FlowGuardRepository) -> None:
        self.repository = repository
        self.tools = CoreToolService(repository)

    def latest(self, user_id: str) -> dict[str, Any]:
        try:
            report = self.repository.latest_report(user_id)
        except RecordNotFound as exc:
            latest_run = self.repository.latest_analysis(user_id)
            details = {"current_analysis": latest_run} if latest_run else {}
            raise ServiceError(
                "SNAPSHOT_NOT_FOUND",
                "완료된 분석 리포트가 없습니다.",
                details=details,
                http_status=404,
            ) from exc
        latest_run = self.repository.latest_analysis(user_id)
        report_run = self.repository.get_analysis(report["analysis_id"])
        interpretation_run = self.repository.latest_interpretation_run(report["analysis_id"])
        revision = self.repository.current_state_revision(user_id)
        report_revision = report.get("current_state_revision")
        is_stale = report_revision != revision
        interpretation_status = (
            interpretation_run["status"]
            if interpretation_run is not None
            else report_run["interpretation_status"]
        )
        interpretation = self._public_interpretation(interpretation_run)
        return {
            **report,
            "refresh_status": latest_run["analysis_status"] if latest_run else "SUCCEEDED",
            "analysis_status": report_run["analysis_status"],
            "interpretation_status": interpretation_status,
            "execution_stage": latest_run["execution_stage"] if latest_run else "COMPLETED",
            "interpretation": interpretation,
            "last_successful_analysis_at": report["created_at"],
            "revision": revision,
            "analysis_revision": report_revision,
            "report_revision": report_revision,
            "latest_data_revision": revision,
            "is_stale": is_stale,
            "analysis_required": (
                is_stale
                or (
                    latest_run is not None
                    and latest_run["analysis_status"] not in {"SUCCEEDED", "SUPERSEDED"}
                )
            ),
        }

    def dashboard(self, user_id: str) -> dict[str, Any]:
        report = self.latest(user_id)
        recommendations = report.get("recommendations", [])
        recommendation = None
        if recommendations:
            recommendation_id = recommendations[0].get("recommendation_id")
            if recommendation_id:
                try:
                    current = self.repository.get_recommendation(user_id, recommendation_id)
                except RecordNotFound:
                    current = None
                if current and current["status"] == "PENDING":
                    recommendation = current
        return {
            "analysis_id": report["analysis_id"],
            "snapshot_id": report["snapshot_id"],
            "as_of": report["as_of"],
            "safe_to_spend": report["safe_to_spend"],
            "presentation": report["presentation"],
            "risk_metrics": report["risk_metrics"],
            "next_risk": report["risk_metrics"],
            "recommendation": recommendation,
            "decision_trace": report.get("agent", {}).get("decision_trace"),
            "data_quality": report["data_quality"],
            "analysis_status": report["analysis_status"],
            "interpretation_status": report["interpretation_status"],
            "execution_stage": report["execution_stage"],
            "interpretation": report["interpretation"],
            "refresh_status": report["refresh_status"],
            "last_successful_analysis_at": report["last_successful_analysis_at"],
            "is_virtual": report.get("is_virtual", False),
            "revision": report["revision"],
            "analysis_revision": report["analysis_revision"],
            "report_revision": report["report_revision"],
            "latest_data_revision": report["latest_data_revision"],
            "is_stale": report["is_stale"],
            "analysis_required": report["analysis_required"],
        }

    @staticmethod
    def _public_interpretation(run: dict[str, Any] | None) -> dict[str, Any] | None:
        if run is None or run.get("response_payload") is None:
            return None
        return {
            **run["response_payload"],
            "source": "DETERMINISTIC_FALLBACK" if run["fallback_used"] else "AI",
            "fallbackReason": run.get("error_code"),
        }

    def cashflow_timeline(self, user_id: str) -> dict[str, Any]:
        report = self.latest(user_id)
        cashflow = report["cashflow"]
        shortfall_type = report["risk_metrics"].get("shortfall_type")
        (
            margin_field,
            balance_basis,
            balance_basis_label,
            requires_reanalysis,
        ) = self._margin_contract(
            cashflow,
            shortfall_type=shortfall_type,
        )
        labels = {
            "ON_TIME": "기준",
            "DELAY_3_DAYS": "3일 지연",
            "DELAY_7_DAYS": "7일 지연",
            "DELAY_14_DAYS": "14일 지연",
        }
        scenarios = [
            {
                **scenario,
                "scenario_label": labels.get(
                    scenario.get("scenario"), scenario.get("scenario", "")
                ),
            }
            for scenario in cashflow["scenarios"]
        ]
        scenario_positions = [scenario["daily_positions"] for scenario in scenarios]
        daily_positions = [
            {
                **position,
                "safety_margin": position[margin_field],
                "worst_case_safety_margin": min(
                    scenario[index][margin_field] for scenario in scenario_positions
                ),
            }
            for index, position in enumerate(cashflow["daily_positions"])
        ]
        event_descriptions: dict[str, str] = {}
        if daily_positions:
            events = self.tools.query_financial_events(
                report["snapshot_id"],
                date_from=self._date(str(self._value(daily_positions[0], "date"))),
                date_to=self._date(str(self._value(daily_positions[-1], "date"))),
            )["events"]
            event_descriptions = {
                str(event["event_id"]): str(event["description"])
                for event in sorted(
                    events,
                    key=lambda event: (
                        event.get("direction") != "OUTFLOW",
                        event.get("expected_date", ""),
                        event.get("event_id", ""),
                    ),
                )
                if event.get("event_id") and event.get("description")
            }
        weekly_positions = self._weekly_positions(
            cashflow,
            margin_field=margin_field,
            event_descriptions=event_descriptions,
        )
        return {
            "analysis_id": report["analysis_id"],
            "snapshot_id": report["snapshot_id"],
            "as_of": report["as_of"],
            "analysis_horizon_days": cashflow["analysis_horizon_days"],
            "horizon_days": cashflow["analysis_horizon_days"],
            "daily_positions": daily_positions,
            "weekly_positions": weekly_positions,
            "scenarios": scenarios,
            "balance_basis": balance_basis,
            "balance_basis_label": balance_basis_label,
            "requires_reanalysis": requires_reanalysis,
            "is_virtual": report.get("is_virtual", False),
        }

    @classmethod
    def _margin_contract(
        cls,
        cashflow: Mapping[str, Any],
        *,
        shortfall_type: str | None,
    ) -> tuple[str, str, str, bool]:
        desired_field = (
            "payment_account_margin" if shortfall_type == "PAYMENT_ACCOUNT" else "liquidity_margin"
        )
        desired_basis = (
            "PAYMENT_ACCOUNT" if shortfall_type == "PAYMENT_ACCOUNT" else "TOTAL_LIQUIDITY"
        )
        positions = list(cashflow.get("daily_positions", ()))
        positions.extend(
            position
            for scenario in cashflow.get("scenarios", ())
            for position in (cls._value(scenario, "daily_positions") or ())
        )
        if positions and all(
            isinstance(cls._value(position, desired_field), (int, float)) for position in positions
        ):
            label = (
                "결제계좌 안전여유"
                if desired_basis == "PAYMENT_ACCOUNT"
                else "전체 유동성 안전여유"
            )
            return desired_field, desired_basis, label, False
        return (
            "available_balance",
            "LEGACY_AVAILABLE_BALANCE",
            "전체 가용잔액 · 다시 분석 필요",
            True,
        )

    @classmethod
    def _weekly_positions(
        cls,
        cashflow: Mapping[str, Any],
        *,
        margin_field: str,
        event_descriptions: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        baseline_positions = list(cashflow.get("daily_positions", ()))
        scenario_positions = [
            list(cls._value(scenario, "daily_positions") or ())
            for scenario in cashflow.get("scenarios", ())
        ]
        if not scenario_positions:
            scenario_positions = [baseline_positions]

        weekly_positions: list[dict[str, Any]] = []
        for start_index in range(0, len(baseline_positions), 7):
            baseline_week = baseline_positions[start_index : start_index + 7]
            all_positions = [
                position
                for positions in scenario_positions
                for position in positions[start_index : start_index + 7]
            ]
            available_balances = [
                value
                for position in all_positions
                if isinstance(
                    (value := cls._value(position, "available_balance")),
                    (int, float),
                )
            ]
            safety_margins = [
                value
                for position in all_positions
                if isinstance(
                    (value := cls._value(position, margin_field)),
                    (int, float),
                )
            ]
            statuses = [
                normalized
                for position in all_positions
                if (normalized := cls._status_value(cls._value(position, "status")))
                in _STATUS_SEVERITY
            ]
            status = max(statuses, key=_STATUS_SEVERITY.__getitem__) if statuses else None
            triggering_event_ids = {
                str(event_id)
                for position in all_positions
                for event_id in (cls._value(position, "triggering_event_ids") or ())
            }
            causes: list[str] = []
            for event_id, description in event_descriptions.items():
                if event_id in triggering_event_ids and description not in causes:
                    causes.append(description)

            weekly_positions.append(
                {
                    "week": start_index // 7 + 1,
                    "start_date": cls._value(baseline_week[0], "date"),
                    "end_date": cls._value(baseline_week[-1], "date"),
                    "min_available_balance": (
                        min(available_balances) if available_balances else None
                    ),
                    "min_safety_margin": min(safety_margins) if safety_margins else None,
                    "status": status,
                    "causes": causes,
                }
            )
        return weekly_positions

    @staticmethod
    def _value(item: Any, field: str) -> Any:
        if isinstance(item, Mapping):
            return item.get(field)
        return getattr(item, field, None)

    @staticmethod
    def _status_value(value: Any) -> str | None:
        if value is None:
            return None
        return str(getattr(value, "value", value))

    def next_risk(self, user_id: str) -> dict[str, Any]:
        report = self.latest(user_id)
        risk_date = report["risk_metrics"].get("first_risk_date")
        triggering_events: list[dict[str, Any]] = []
        if risk_date:
            positions = report["cashflow"]["daily_positions"]
            position = next((item for item in positions if item["date"] == risk_date), None)
            triggering_ids = set(position.get("triggering_event_ids", []) if position else [])
            events = self.tools.query_financial_events(
                report["snapshot_id"],
                date_from=self._date(risk_date),
                date_to=self._date(risk_date),
            )["events"]
            triggering_events = [event for event in events if event["event_id"] in triggering_ids]
        return {
            "analysis_id": report["analysis_id"],
            "snapshot_id": report["snapshot_id"],
            "as_of": report["as_of"],
            "presentation": report["presentation"],
            "risk_metrics": report["risk_metrics"],
            "data_quality": report["data_quality"],
            "triggering_events": triggering_events,
            "causes": [report["presentation"]["cause"]],
        }

    def receivables(self, user_id: str) -> list[dict[str, Any]]:
        receivables = self.repository.list_records(user_id, "receivables")
        counterparties = {
            item["counterparty_id"]: item
            for item in self.repository.list_records(user_id, "counterparties")
        }
        try:
            report = self.repository.latest_report(user_id)
        except RecordNotFound:
            report = None
        enriched: list[dict[str, Any]] = []
        for receivable in receivables:
            evidence = None
            if report is not None:
                try:
                    evidence = self.tools.get_counterparty_evidence(
                        report["snapshot_id"], receivable["counterparty_id"]
                    )
                except ServiceError as exc:
                    if exc.code != "INSUFFICIENT_DATA":
                        raise
            counterparty = counterparties.get(receivable["counterparty_id"], {})
            enriched.append(
                {
                    **receivable,
                    "counterparty_name": counterparty.get("name"),
                    "counterparty_evidence": evidence,
                }
            )
        return enriched

    @staticmethod
    def _date(value: str):
        from datetime import date

        return date.fromisoformat(value)
