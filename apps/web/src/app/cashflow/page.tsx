"use client";

import Link from "next/link";

import { Icon } from "@/components/icons";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageIntro,
  SectionHeading,
  StatusPill,
} from "@/components/ui";
import { ApiError, useRemote } from "@/lib/api";
import {
  deriveWeeklyPositions,
  formatCompactWon,
  formatDate,
  formatShortDate,
  formatWon,
  normalizeStatus,
  worstCaseSafetyMargin,
} from "@/lib/format";
import type {
  DailyPosition,
  NextRiskResponse,
  TimelineResponse,
  WeeklyPosition,
} from "@/lib/types";

function weekNumber(position: WeeklyPosition, index: number) {
  return typeof position.week === "number" ? position.week : index + 1;
}

/** 백엔드 위험일이 없을 때만 최악 시나리오 안전여유가 처음 음수가 되는 날을 쓴다. */
function firstShortfallDate(daily: DailyPosition[]) {
  return daily.find((position) => {
    const margin = worstCaseSafetyMargin(position);
    return typeof margin === "number" && margin < 0;
  })?.date;
}

function coversDate(position: WeeklyPosition, date: string) {
  if (!position.start_date || !position.end_date) return false;
  return position.start_date <= date && date <= position.end_date;
}

function weeklySafetyMargin(position: WeeklyPosition) {
  if (typeof position.min_safety_margin === "number") {
    return position.min_safety_margin;
  }
  return position.min_available_balance;
}

function chartHeight(value: number, maximum: number) {
  return Math.max(value === 0 ? 2 : 4, (Math.abs(value) / maximum) * 44);
}

function scenarioName(scenario: NonNullable<TimelineResponse["scenarios"]>[number]) {
  if (typeof scenario === "string") return scenario;
  const raw =
    scenario.scenario_label ||
    scenario.label ||
    scenario.name ||
    scenario.scenario ||
    scenario.id;
  const labels: Record<string, string> = {
    ON_TIME: "예정일 입금",
    DELAY_3_DAYS: "3일 지연",
    DELAY_7_DAYS: "7일 지연",
    DELAY_14_DAYS: "14일 지연",
  };
  return raw ? labels[raw] || raw : "이름 정보 없음";
}

type DayRow =
  | { kind: "day"; position: DailyPosition; index: number }
  | { kind: "quiet"; from: string; to: string; days: number };

/**
 * 변동 없는 날이 연달아 이어지면 표가 같은 값으로 채워져 스크롤만 길어진다.
 * 이벤트가 있거나 상태가 바뀌거나 위험일인 날만 남기고 나머지는 한 줄로 묶는다.
 */
function collapseQuietDays(
  positions: DailyPosition[],
  riskDate?: string,
): DayRow[] {
  const rows: DayRow[] = [];
  let quiet: DailyPosition[] = [];

  const flush = () => {
    if (!quiet.length) return;
    if (quiet.length <= 2) {
      quiet.forEach((position) =>
        rows.push({ kind: "day", position, index: positions.indexOf(position) }),
      );
    } else {
      rows.push({
        kind: "quiet",
        from: quiet[0].date,
        to: quiet[quiet.length - 1].date,
        days: quiet.length,
      });
    }
    quiet = [];
  };

  positions.forEach((position, index) => {
    const hasEvent = Boolean(position.triggering_event_ids?.length);
    const isRisk = Boolean(riskDate) && position.date === riskDate;
    const statusChanged =
      index > 0 && position.status !== positions[index - 1].status;
    const isEdge = index === 0 || index === positions.length - 1;

    if (hasEvent || isRisk || statusChanged || isEdge) {
      flush();
      rows.push({ kind: "day", position, index });
    } else {
      quiet.push(position);
    }
  });
  flush();
  return rows;
}

export default function CashflowPage() {
  const { data, error, loading, reload } =
    useRemote<TimelineResponse>("/api/v1/cashflow/timeline");
  const { data: nextRisk } = useRemote<NextRiskResponse>("/api/v1/risks/next");

  const daily = data?.daily_positions || [];
  const firstFourWeeks = daily.slice(0, 28);
  const weekly = data?.weekly_positions?.length
    ? data.weekly_positions
    : deriveWeeklyPositions(daily);
  const balanceBasisLabel =
    data?.balance_basis_label || "최악 입금 지연 시나리오의 결제 안전여유";
  const laterWeeks = weekly.filter(
    (position, index) => weekNumber(position, index) >= 5,
  );
  const firstFourMargins = firstFourWeeks
    .map(worstCaseSafetyMargin)
    .filter((value): value is number => typeof value === "number");
  const maxAbsoluteMargin = Math.max(
    1,
    ...firstFourMargins.map((margin) => Math.abs(margin)),
  );

  const riskDate =
    nextRisk?.risk_metrics?.first_risk_date || firstShortfallDate(daily);
  const riskIsDerived = Boolean(
    riskDate && !nextRisk?.risk_metrics?.first_risk_date,
  );
  const riskInChart = Boolean(
    riskDate && firstFourWeeks.some((position) => position.date === riskDate),
  );
  const riskWeek = riskDate
    ? weekly.find((position) => coversDate(position, riskDate))
    : undefined;
  const riskStatus = normalizeStatus(nextRisk?.presentation?.status);
  const riskWeekIndex = riskDate
    ? weekly.findIndex((position) => coversDate(position, riskDate))
    : -1;

  const overviewMargins = weekly
    .map(weeklySafetyMargin)
    .filter((value): value is number => typeof value === "number");
  const overviewMaximum = Math.max(
    1,
    ...overviewMargins.map((margin) => Math.abs(margin)),
  );
  const overviewMin = overviewMargins.length
    ? Math.min(...overviewMargins)
    : undefined;

  if (loading && !data) return <LoadingState label="13주 현금흐름을 펼치고 있어요" />;
  if (error instanceof ApiError && error.status === 404 && !data) {
    return (
      <EmptyState
        icon="cashflow"
        title="아직 현금흐름 분석이 없어요"
        description="거래 CSV를 연결하고 첫 13주 분석을 시작해 주세요."
        action={
          <Link className="button button-primary" href="/setup">
            처음 설정 시작
          </Link>
        }
      />
    );
  }
  if (error && !data) return <ErrorState error={error} onRetry={reload} />;
  if (!data || !daily.length) {
    return (
      <EmptyState
        icon="cashflow"
        title="표시할 날짜별 현금흐름이 없어요"
        description="분석 결과에 daily_positions가 준비되면 1~4주 상세 흐름과 이후 위험구간을 보여드립니다."
        action={
          <button className="button button-secondary" onClick={reload} type="button">
            <Icon name="refresh" size={17} />
            다시 확인
          </button>
        }
      />
    );
  }

  return (
    <>
      <PageIntro
        eyebrow={`${data.horizon_days || daily.length}일 분석`}
        title="가까운 날은 자세히, 먼 날은 흐름으로."
        description="1~4주는 날짜별 잔액과 결제를 확인하고, 5~13주는 위험이 모이는 구간에 집중합니다."
        action={
          <div className="as-of-chip">
            <Icon name="refresh" size={16} />
            분석 기준 {formatDate(data.as_of, true)}
          </div>
        }
      />

      {data.scenarios?.length ? (
        <div className="scenario-strip" aria-label="분석 시나리오">
          <span>반영 시나리오</span>
          {data.scenarios.map((scenario, index) => (
            <span className="scenario-chip" key={`${scenarioName(scenario)}-${index}`}>
              {scenarioName(scenario)}
            </span>
          ))}
        </div>
      ) : null}

      <section>
        <SectionHeading
          eyebrow="13주 전체"
          title="언제 부족해지는지 한눈에"
          description={`${balanceBasisLabel}을 주차별로 보여드립니다. 0원 아래는 결제 안전기준을 충족하지 못하는 구간입니다.`}
          aside={
            <div className="chart-legend">
              <span>
                <i className="legend-stable" /> 안전
              </span>
              <span>
                <i className="legend-verify" /> 확인
              </span>
              <span>
                <i className="legend-prepare" /> 준비
              </span>
              <span>
                <i className="legend-act-now" /> 조치
              </span>
              {riskDate && (
                <span>
                  <i className="legend-marker" /> 위험 시점
                </span>
              )}
            </div>
          }
        />

        {riskDate && (
          <div
            className={`risk-callout risk-callout-${riskStatus?.toLowerCase() || "unknown"}`}
          >
            <span className="risk-callout-icon">
              <Icon name="calendar" size={19} />
            </span>
            <div>
              <strong>
                {formatDate(riskDate)}에 첫 위험이 있어요
                {riskWeek?.week ? ` · ${riskWeek.week}주 차` : ""}
              </strong>
              <p>
                {nextRisk?.presentation?.impact ||
                  "표시된 시점부터 준비가 필요한 결제가 있습니다."}
              </p>
              {riskIsDerived && (
                <small>
                  분석 결과에 위험일이 없어 최악 시나리오 안전여유가 처음 0원 아래로
                  내려가는 날짜로 표시했습니다.
                </small>
              )}
              {!riskInChart && (
                <small>
                  아래 1~4주 상세 그래프 범위 밖이라 주차 카드에도 표시했습니다.
                </small>
              )}
            </div>
            <Link className="inline-link" href="/risk">
              위험 근거 보기
              <Icon name="arrow" size={16} />
            </Link>
          </div>
        )}

        <div className="overview-chart card">
          <div
            className="overview-bars"
            style={{ gridTemplateColumns: `repeat(${weekly.length}, minmax(0, 1fr))` }}
            aria-label={`${weekly.length}주 결제 안전여유 흐름 그래프`}
          >
            <span className="overview-zero-line" aria-hidden="true" />
            {weekly.map((position, index) => {
              const margin = weeklySafetyMargin(position);
              const height =
                typeof margin === "number" ? chartHeight(margin, overviewMaximum) : 4;
              const status = normalizeStatus(position.status);
              const isNegative = typeof margin === "number" && margin < 0;
              const week = weekNumber(position, index);
              return (
                <span className="overview-bar-column" key={`overview-${week}-${index}`}>
                  <span
                    className={`overview-bar status-${
                      status?.toLowerCase() || "unknown"
                    } ${isNegative ? "is-negative" : "is-positive"}${
                      typeof margin !== "number" ? " unknown" : ""
                    }`}
                    style={{ height: `${height}%` }}
                    title={
                      typeof margin === "number"
                        ? `${week}주 차 · ${balanceBasisLabel} ${formatWon(margin)}`
                        : `${week}주 차 · 안전여유 정보 없음`
                    }
                  />
                </span>
              );
            })}

            {riskWeekIndex >= 0 && (
              <span
                className="overview-risk-marker"
                style={{ left: `${((riskWeekIndex + 0.5) / weekly.length) * 100}%` }}
              >
                <span className="overview-risk-flag">
                  {formatShortDate(riskDate)} 첫 위험
                </span>
              </span>
            )}
          </div>

          <div className="overview-axis" aria-hidden="true">
            {[0, 4, 8, 12].map((week) => {
              const position = weekly[week];
              if (!position) return null;
              return (
                <span
                  key={`tick-${week}`}
                  style={{ left: `${((week + 0.5) / weekly.length) * 100}%` }}
                >
                  {weekNumber(position, week)}주
                </span>
              );
            })}
          </div>

          <div className="overview-facts">
            <div>
              <small>기간 내 최소 안전여유</small>
              <strong>
                {typeof overviewMin === "number"
                  ? formatWon(overviewMin)
                  : "정보 없음"}
              </strong>
            </div>
            <div>
              <small>분석 기간</small>
              <strong>
                {formatShortDate(daily.at(0)?.date)} –{" "}
                {formatShortDate(daily.at(-1)?.date)}
              </strong>
            </div>
          </div>
        </div>
      </section>

      <section>
        <SectionHeading
          eyebrow="1–4주"
          title="날짜별 결제 안전여유"
          description={`막대는 ${balanceBasisLabel}입니다. 0원 아래로 내려가면 결제 전에 자금 준비가 필요합니다.`}
          aside={
            <div className="chart-legend">
              <span>
                <i className="legend-safe" /> 안전기준 이상
              </span>
              <span>
                <i className="legend-risk" /> 안전기준 미달
              </span>
              {riskDate && (
                <span>
                  <i className="legend-marker" /> 위험 시점
                </span>
              )}
            </div>
          }
        />

        <div className="cashflow-chart card">
          <div className="chart-zero-line">
            <span>0원</span>
          </div>
          <div className="bars" aria-label="1~4주 최악 시나리오 안전여유 막대 그래프">
            {firstFourWeeks.map((position, index) => {
              const margin = worstCaseSafetyMargin(position);
              const height =
                typeof margin === "number" ? chartHeight(margin, maxAbsoluteMargin) : 4;
              const isNegative = typeof margin === "number" && margin < 0;
              const isRiskDay = Boolean(riskDate) && position.date === riskDate;
              return (
                <div
                  className={`bar-column${isRiskDay ? " is-risk-day" : ""}`}
                  key={`${position.date}-${index}`}
                >
                  {isRiskDay && (
                    <span className="risk-marker">
                      <span className="risk-marker-flag">위험</span>
                      <span className="risk-marker-line" aria-hidden="true" />
                    </span>
                  )}
                  <span
                    className={`cashflow-bar ${
                      isNegative ? "negative is-negative" : "is-positive"
                    } ${typeof margin !== "number" ? "unknown" : ""}`}
                    style={{ height: `${height}%` }}
                    title={
                      typeof margin === "number"
                        ? `${formatDate(position.date)} ${balanceBasisLabel} ${formatWon(margin)}${
                            isRiskDay ? " · 첫 위험 시점" : ""
                          }`
                        : `${formatDate(position.date)} 안전여유 정보 없음`
                    }
                  />
                  {(index % 7 === 0 || index === firstFourWeeks.length - 1) && (
                    <small>{formatShortDate(position.date)}</small>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        <div className="cashflow-table card card-flat">
          <div className="cashflow-table-head">
            <span>날짜</span>
            <span>영향 이벤트</span>
            <span>전체 잔액</span>
            <span title={balanceBasisLabel}>안전여유</span>
            <span>상태</span>
          </div>
          <div className="cashflow-table-body">
            {collapseQuietDays(firstFourWeeks, riskDate).map((row) => {
              if (row.kind === "quiet") {
                return (
                  <div
                    className="cashflow-row is-quiet-row"
                    key={`quiet-${row.from}-${row.to}`}
                  >
                    <strong>
                      {formatShortDate(row.from)} – {formatShortDate(row.to)}
                    </strong>
                    <span>{row.days}일 동안 변동 없음</span>
                    <span aria-hidden="true">·</span>
                    <span aria-hidden="true">·</span>
                    <span aria-hidden="true">·</span>
                  </div>
                );
              }
              const { position, index } = row;
              const margin = worstCaseSafetyMargin(position);
              return (
                <div
                  className={`cashflow-row${
                    riskDate && position.date === riskDate ? " is-risk-row" : ""
                  }`}
                  key={`${position.date}-row-${index}`}
                >
                  <strong>{formatDate(position.date)}</strong>
                  <span>
                    {position.triggering_event_ids?.length
                      ? `${position.triggering_event_ids.length}개 이벤트`
                      : "영향 이벤트 없음"}
                  </span>
                  <span>
                    {typeof position.total_balance === "number"
                      ? formatWon(position.total_balance)
                      : "정보 없음"}
                  </span>
                  <strong
                    className={
                      typeof margin === "number" && margin < 0
                        ? "negative-money"
                        : ""
                    }
                  >
                    {typeof margin === "number" ? formatWon(margin) : "정보 없음"}
                  </strong>
                  <StatusPill status={position.status} />
                </div>
              );
            })}
          </div>
        </div>
      </section>

      <section>
        <SectionHeading
          eyebrow="5–13주"
          title="멀리 있는 위험구간"
          description="세금·할부·고정비가 겹치는 방향과 주차별 최소 안전여유를 보여드려요."
        />

        {laterWeeks.length ? (
          <div className="week-grid">
            {laterWeeks.map((position, index) => {
              const sourceIndex = weekly.indexOf(position);
              const week = weekNumber(position, sourceIndex);
              const status = normalizeStatus(position.status);
              const holdsRisk = Boolean(riskDate) && position === riskWeek;
              const margin = weeklySafetyMargin(position);
              const causes = (position.causes || []).filter((cause) => cause.trim());
              const fallbackCause =
                status === "STABLE" && !holdsRisk
                  ? "예정된 주요 위험 없음"
                  : "원인 확인 필요";
              // 안정 구간까지 모두 펼치면 모바일에서 카드가 9개 넘게 쌓인다.
              // 주의가 필요한 주차만 펼치고 나머지는 한 줄 요약으로 접는다.
              const isQuietWeek = status === "STABLE" && !holdsRisk;
              if (isQuietWeek) {
                return (
                  <article
                    className="week-card is-quiet-week"
                    key={`${week}-${position.start_date || index}`}
                  >
                    <div className="week-card-top">
                      <span>{week}주 차</span>
                      <StatusPill status={position.status} />
                    </div>
                    <p>
                      {formatShortDate(position.start_date)} –{" "}
                      {formatShortDate(position.end_date)}
                      {typeof margin === "number"
                        ? ` · 최소 ${formatCompactWon(margin)}`
                        : ""}
                    </p>
                  </article>
                );
              }
              return (
                <article
                  className={`week-card week-${status?.toLowerCase() || "unknown"}${
                    holdsRisk ? " is-risk-week" : ""
                  }`}
                  key={`${week}-${position.start_date || index}`}
                >
                  <div className="week-card-top">
                    <span>{week}주 차</span>
                    <StatusPill status={position.status} />
                  </div>
                  {holdsRisk && riskDate && (
                    <span className="week-risk-flag">
                      <Icon name="calendar" size={13} />
                      {formatShortDate(riskDate)} 첫 위험
                    </span>
                  )}
                  <p>
                    {formatShortDate(position.start_date)} –{" "}
                    {formatShortDate(position.end_date)}
                  </p>
                  <strong
                    className={
                      typeof margin === "number" && margin < 0
                        ? "negative-money"
                        : ""
                    }
                  >
                    {typeof margin === "number"
                      ? formatCompactWon(margin)
                      : "안전여유 정보 없음"}
                  </strong>
                  <small>구간 내 최소 안전여유</small>
                  <div className="week-causes">
                    {causes.length ? (
                      causes.slice(0, 2).map((cause) => (
                        <span key={cause}>{cause}</span>
                      ))
                    ) : (
                      <span>{fallbackCause}</span>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        ) : (
          <EmptyState
            icon="calendar"
            title="5~13주 요약이 아직 없어요"
            description="API가 전체 91일 데이터 또는 weekly_positions를 반환하면 이후 구간을 표시합니다."
          />
        )}
      </section>
    </>
  );
}
