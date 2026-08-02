"""Read models for the feature-oriented FlowGuard screens."""

from __future__ import annotations

from typing import Any

from flowguard.storage import FlowGuardRepository, RecordNotFound

from .errors import ServiceError
from .tools import CoreToolService


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
        return {
            "analysis_id": report["analysis_id"],
            "snapshot_id": report["snapshot_id"],
            "as_of": report["as_of"],
            "analysis_horizon_days": cashflow["analysis_horizon_days"],
            "horizon_days": cashflow["analysis_horizon_days"],
            "daily_positions": cashflow["daily_positions"],
            "scenarios": scenarios,
            "is_virtual": report.get("is_virtual", False),
        }

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
