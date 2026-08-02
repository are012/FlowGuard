import type {
  ActionDefinition,
  EvaluationState,
  Recommendation,
  RiskStatus,
  WeeklyPosition,
  DailyPosition,
} from "@/lib/types";

const wonFormatter = new Intl.NumberFormat("ko-KR", {
  maximumFractionDigits: 0,
});

export function formatWon(value: number) {
  return `${wonFormatter.format(value)}원`;
}

export function formatCompactWon(value: number) {
  const absolute = Math.abs(value);
  if (absolute >= 100_000_000) {
    return `${(value / 100_000_000).toFixed(1).replace(/\.0$/, "")}억원`;
  }
  if (absolute >= 10_000) {
    return `${(value / 10_000).toFixed(0)}만원`;
  }
  return formatWon(value);
}

function toDate(value?: string | null) {
  if (!value) return null;
  const normalized = /^\d{4}-\d{2}-\d{2}$/.test(value)
    ? `${value}T00:00:00+09:00`
    : value;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDate(value?: string | null, includeYear = false) {
  const date = toDate(value);
  if (!date) return "날짜 정보 없음";
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul",
    year: includeYear ? "numeric" : undefined,
    month: "long",
    day: "numeric",
    weekday: "short",
  }).format(date);
}

export function formatShortDate(value?: string | null) {
  const date = toDate(value);
  if (!date) return "—";
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul",
    month: "numeric",
    day: "numeric",
  }).format(date);
}

export function formatDateTime(value?: string | null) {
  const date = toDate(value);
  if (!date) return "업데이트 정보 없음";
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul",
    month: "long",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatPercent(value?: number) {
  if (typeof value !== "number") return "정보 없음";
  return `${Math.round(value * 100)}%`;
}

export const statusLabel: Record<RiskStatus, string> = {
  STABLE: "현재는 안전해요",
  VERIFY: "확인할 정보가 있어요",
  PREPARE: "미리 준비가 필요해요",
  ACT_NOW: "지금 조치가 필요해요",
};

export function normalizeStatus(value?: string): RiskStatus | undefined {
  if (value === "STABLE" || value === "VERIFY" || value === "PREPARE" || value === "ACT_NOW") {
    return value;
  }
  return undefined;
}

export function worstCaseSafetyMargin(position: DailyPosition) {
  if (typeof position.worst_case_safety_margin === "number") {
    return position.worst_case_safety_margin;
  }
  if (typeof position.safety_margin === "number") return position.safety_margin;
  return position.available_balance;
}

export function recommendationId(item: Recommendation) {
  return item.recommendation_id || item.id;
}

export function recommendationActions(item: Recommendation) {
  if (item.actions?.length) return item.actions;
  return item.action ? [item.action] : [];
}

export function evaluationBefore(item: Recommendation): EvaluationState | undefined {
  return item.evaluation?.before || item.before;
}

export function evaluationAfter(item: Recommendation): EvaluationState | undefined {
  return item.evaluation?.after || item.after;
}

export function actionLabel(action?: ActionDefinition) {
  if (!action) return "행동 정보 없음";
  const parameters = action.parameters || {};
  const amount =
    typeof parameters.amount === "number"
      ? formatWon(parameters.amount)
      : typeof parameters.purchase_amount === "number"
        ? formatWon(parameters.purchase_amount)
        : null;

  switch (action.type) {
    case "transfer":
      return `${amount || "필요한 금액"}을 결제계좌로 옮기기`;
    case "reserve_funds":
      return `${amount || "필요한 금액"}을 목적별 자금으로 지키기`;
    case "adjust_discretionary_budget":
      return `선택지출을 ${amount || "필요한 만큼"} 조정하기`;
    case "pause_savings":
      return "조정 가능한 자동저축을 잠시 멈추기";
    case "shift_payment_date":
      return "결제일 변경 효과를 가상으로 확인하기";
    case "delay_purchase":
      return "구매 시점을 늦추기";
    case "add_installment":
      return `${amount || "구매"} 할부 조건을 미리 확인하기`;
    case "confirm_receivable":
      return "예정 수입 일정을 확인하기";
    default:
      return action.type || "추천 행동";
  }
}

export function deriveWeeklyPositions(daily: DailyPosition[]): WeeklyPosition[] {
  const groups = new Map<number, DailyPosition[]>();
  daily.forEach((position, index) => {
    const week = Math.floor(index / 7) + 1;
    groups.set(week, [...(groups.get(week) || []), position]);
  });

  return Array.from(groups, ([week, positions]) => {
    const available = positions
      .map((position) => position.available_balance)
      .filter((value): value is number => typeof value === "number");
    const safetyMargins = positions
      .map(worstCaseSafetyMargin)
      .filter((value): value is number => typeof value === "number");
    const order: RiskStatus[] = ["STABLE", "VERIFY", "PREPARE", "ACT_NOW"];
    const statuses = positions
      .map((position) => normalizeStatus(position.status))
      .filter((value): value is RiskStatus => Boolean(value));
    const status = statuses.length
      ? statuses.reduce((worst, current) =>
          order.indexOf(current) > order.indexOf(worst) ? current : worst,
        )
      : undefined;

    return {
      week,
      start_date: positions.at(0)?.date,
      end_date: positions.at(-1)?.date,
      min_available_balance: available.length ? Math.min(...available) : undefined,
      min_safety_margin: safetyMargins.length ? Math.min(...safetyMargins) : undefined,
      status,
      causes: [],
    };
  });
}

export function candidateTypeLabel(type?: string) {
  const labels: Record<string, string> = {
    RECURRING_INCOME: "반복 수입",
    FIXED_EXPENSE: "고정지출",
    INSTALLMENT: "할부",
    recurring_income: "반복 수입",
    fixed_expense: "고정지출",
    installment: "할부",
  };
  return type ? labels[type] || type : "분류 후보";
}
