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

/** 백엔드가 계산한 위험일이 없을 때만 가용잔액이 처음 0원 아래로 내려가는 날을 쓴다. */
function firstShortfallDate(daily: DailyPosition[]) {
  return daily.find(
    (position) =>
      typeof position.available_balance === "number" &&
      position.available_balance < 0,
  )?.date;
}

function coversDate(position: WeeklyPosition, date: string) {
  if (!position.start_date || !position.end_date) return false;
  return position.start_date <= date && date <= position.end_date;
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

export default function CashflowPage() {
  const { data, error, loading, reload } =
    useRemote<TimelineResponse>("/api/v1/cashflow/timeline");
  const { data: nextRisk } = useRemote<NextRiskResponse>("/api/v1/risks/next");

  const daily = data?.daily_positions || [];
  const firstFourWeeks = daily.slice(0, 28);
  const weekly = data?.weekly_positions?.length
    ? data.weekly_positions
    : deriveWeeklyPositions(daily);
  const laterWeeks = weekly.filter(
    (position, index) => weekNumber(position, index) >= 5,
  );
  const maxAbsoluteBalance = Math.max(
    1,
    ...firstFourWeeks.map((position) =>
      Math.abs(position.available_balance || 0),
    ),
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
  const riskIndex = riskDate
    ? daily.findIndex((position) => position.date === riskDate)
    : -1;

  const overviewBalances = daily
    .map((position) => position.available_balance)
    .filter((value): value is number => typeof value === "number");
  const overviewMax = Math.max(1, ...overviewBalances);
  const overviewMin = overviewBalances.length ? Math.min(...overviewBalances) : undefined;

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
          description={`분석 기간 ${daily.length}일의 가용잔액 흐름입니다. 위험 시점이 전체 흐름에서 어디에 있는지 먼저 확인하세요.`}
          aside={
            <div className="chart-legend">
              <span><i className="legend-safe" /> 위험 이전</span>
              {riskDate && <span><i className="legend-after-risk" /> 위험 이후</span>}
              {riskDate && <span><i className="legend-marker" /> 위험 시점</span>}
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
                  분석 결과에 위험일이 없어 가용잔액이 처음 0원 아래로 내려가는
                  날짜로 표시했습니다.
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
            style={{ gridTemplateColumns: `repeat(${daily.length}, minmax(0, 1fr))` }}
            aria-label={`${daily.length}일 가용잔액 흐름 그래프`}
          >
            {daily.map((position, index) => {
              const balance = position.available_balance;
              const height =
                typeof balance === "number"
                  ? Math.max(6, (balance / overviewMax) * 100)
                  : 6;
              const isRiskDay = index === riskIndex;
              const isAfterRisk = riskIndex >= 0 && index > riskIndex;
              return (
                <span
                  className={`overview-bar${isRiskDay ? " is-risk" : ""}${
                    isAfterRisk ? " is-after-risk" : ""
                  }${typeof balance !== "number" ? " unknown" : ""}`}
                  key={`overview-${position.date}-${index}`}
                  style={{ height: `${height}%` }}
                  title={
                    typeof balance === "number"
                      ? `${formatDate(position.date)} ${formatWon(balance)}${
                          isRiskDay ? " · 첫 위험 시점" : ""
                        }`
                      : `${formatDate(position.date)} 잔액 정보 없음`
                  }
                />
              );
            })}

            {riskIndex >= 0 && (
              <span
                className="overview-risk-marker"
                style={{ left: `${((riskIndex + 0.5) / daily.length) * 100}%` }}
              >
                <span className="overview-risk-flag">
                  {formatShortDate(riskDate)} 첫 위험
                </span>
              </span>
            )}
          </div>

          <div className="overview-axis" aria-hidden="true">
            {[0, 4, 8, 12].map((week) => {
              const position = daily[week * 7];
              if (!position) return null;
              return (
                <span
                  key={`tick-${week}`}
                  style={{ left: `${((week * 7) / daily.length) * 100}%` }}
                >
                  {week + 1}주
                </span>
              );
            })}
          </div>

          <div className="overview-facts">
            <div>
              <small>기간 내 최소 가용잔액</small>
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
          title="날짜별 예상 가용잔액"
          description="막대는 API가 계산한 매일의 가용잔액이며, 아래 표에서 결제 영향을 함께 확인할 수 있어요."
          aside={
            <div className="chart-legend">
              <span><i className="legend-safe" /> 가용자금</span>
              <span><i className="legend-risk" /> 결제 안전기준 미달</span>
              {riskDate && <span><i className="legend-marker" /> 위험 시점</span>}
            </div>
          }
        />

        <div className="cashflow-chart card">
          <div className="chart-zero-line">
            <span>0원</span>
          </div>
          <div className="bars" aria-label="1~4주 가용잔액 막대 그래프">
            {firstFourWeeks.map((position, index) => {
              const balance = position.available_balance;
              const height =
                typeof balance === "number"
                  ? Math.max(5, (Math.abs(balance) / maxAbsoluteBalance) * 76)
                  : 5;
              const positionStatus = normalizeStatus(position.status);
              const belowSafety =
                (typeof balance === "number" && balance < 0) ||
                positionStatus === "PREPARE" ||
                positionStatus === "ACT_NOW";
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
                    className={`cashflow-bar ${belowSafety ? "negative" : ""} ${
                      typeof balance !== "number" ? "unknown" : ""
                    }`}
                    style={{ height: `${height}%` }}
                    title={
                      typeof balance === "number"
                        ? `${formatDate(position.date)} ${formatWon(balance)}${
                            isRiskDay ? " · 첫 위험 시점" : ""
                          }`
                        : `${formatDate(position.date)} 잔액 정보 없음`
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
            <span>가용잔액</span>
            <span>상태</span>
          </div>
          <div className="cashflow-table-body">
            {firstFourWeeks.map((position: DailyPosition, index) => (
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
                    typeof position.available_balance === "number" &&
                    position.available_balance < 0
                      ? "negative-money"
                      : ""
                  }
                >
                  {typeof position.available_balance === "number"
                    ? formatWon(position.available_balance)
                    : "정보 없음"}
                </strong>
                <StatusPill status={position.status} />
              </div>
            ))}
          </div>
        </div>
      </section>

      <section>
        <SectionHeading
          eyebrow="5–13주"
          title="멀리 있는 위험구간"
          description="정밀한 숫자보다 세금·할부·고정비가 겹치는 방향을 주차별로 보여드려요."
        />

        {laterWeeks.length ? (
          <div className="week-grid">
            {laterWeeks.map((position, index) => {
              const sourceIndex = weekly.indexOf(position);
              const week = weekNumber(position, sourceIndex);
              const status = normalizeStatus(position.status);
              const holdsRisk = Boolean(riskDate) && position === riskWeek;
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
                  <strong>
                    {typeof position.min_available_balance === "number"
                      ? formatCompactWon(position.min_available_balance)
                      : "가용잔액 정보 없음"}
                  </strong>
                  <small>구간 내 최소 가용잔액</small>
                  <div className="week-causes">
                    {position.causes?.length ? (
                      position.causes.slice(0, 2).map((cause) => (
                        <span key={cause}>{cause}</span>
                      ))
                    ) : (
                      <span>원인 정보 없음</span>
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
