"""Deterministic counterparty payment evidence calculations."""

from __future__ import annotations

from statistics import fmean, median

from flowguard.config import MIN_COUNTERPARTY_HISTORY_COUNT
from flowguard.domain import CounterpartyEvidence, FinancialSnapshot, RecentTrend


def get_counterparty_evidence(
    snapshot: FinancialSnapshot,
    counterparty_id: str,
) -> CounterpartyEvidence:
    """Summarize observed payment delays for one counterparty."""

    known_counterparties = {
        counterparty.counterparty_id for counterparty in snapshot.counterparties
    }
    histories = sorted(
        (
            history
            for history in snapshot.payment_histories
            if history.counterparty_id == counterparty_id
        ),
        key=lambda history: (history.actual_date, history.payment_history_id),
    )
    if counterparty_id not in known_counterparties and not histories:
        raise ValueError(f"counterparty not found: {counterparty_id}")

    delays = [history.delay_days for history in histories]
    count = len(delays)
    if count == 0:
        return CounterpartyEvidence(
            counterparty_id=counterparty_id,
            payment_history_count=0,
            on_time_rate=0,
            average_delay_days=0,
            median_delay_days=0,
            maximum_delay_days=0,
            recent_trend=RecentTrend.INSUFFICIENT_DATA,
            data_confidence=0,
        )

    if count < MIN_COUNTERPARTY_HISTORY_COUNT:
        trend = RecentTrend.INSUFFICIENT_DATA
    else:
        split = max(1, count // 2)
        older_average = fmean(delays[:split])
        recent_average = fmean(delays[split:]) if delays[split:] else older_average
        if recent_average > older_average + 1:
            trend = RecentTrend.WORSENING
        elif recent_average < older_average - 1:
            trend = RecentTrend.IMPROVING
        else:
            trend = RecentTrend.STABLE

    return CounterpartyEvidence(
        counterparty_id=counterparty_id,
        payment_history_count=count,
        on_time_rate=sum(delay == 0 for delay in delays) / count,
        average_delay_days=fmean(delays),
        median_delay_days=float(median(delays)),
        maximum_delay_days=max(delays),
        recent_trend=trend,
        data_confidence=min(1.0, count / MIN_COUNTERPARTY_HISTORY_COUNT),
    )
