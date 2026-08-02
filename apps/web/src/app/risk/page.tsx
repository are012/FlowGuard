"use client";

import Link from "next/link";

import { Icon } from "@/components/icons";
import {
  DataFact,
  EmptyState,
  ErrorState,
  LoadingState,
  PageIntro,
  SectionHeading,
  StatusPill,
} from "@/components/ui";
import { ApiError, useRemote } from "@/lib/api";
import { confidenceProgressCopy } from "@/lib/confidence";
import {
  formatDate,
  formatDateTime,
  formatPercent,
  formatWon,
} from "@/lib/format";
import type { NextRiskResponse } from "@/lib/types";

function recordString(record: Record<string, unknown>, ...keys: string[]) {
  for (const key of keys) {
    if (typeof record[key] === "string") return record[key] as string;
  }
  return undefined;
}

function recordNumber(record: Record<string, unknown>, ...keys: string[]) {
  for (const key of keys) {
    if (typeof record[key] === "number") return record[key] as number;
  }
  return undefined;
}

export default function RiskPage() {
  const { data, error, loading, reload } =
    useRemote<NextRiskResponse>("/api/v1/risks/next");

  if (loading && !data) return <LoadingState label="가장 가까운 결제 위험을 찾고 있어요" />;
  if (error instanceof ApiError && error.status === 404 && !data) {
    return (
      <EmptyState
        icon="risk"
        title="아직 확인할 분석이 없어요"
        description="첫 거래 데이터를 연결하면 가장 가까운 결제 위험부터 찾아드립니다."
        action={
          <Link className="button button-primary" href="/setup">
            처음 설정 시작
          </Link>
        }
      />
    );
  }
  if (error && !data) return <ErrorState error={error} onRetry={reload} />;
  if (!data || (!data.risk_metrics && !data.presentation)) {
    return (
      <EmptyState
        icon="risk"
        title="가장 가까운 위험 정보가 없어요"
        description="API 분석 결과에 risk_metrics 또는 presentation이 준비되면 상세 근거를 표시합니다."
      />
    );
  }

  const metrics = data.risk_metrics || {};
  const presentation = data.presentation || {};
  const confidence = data.data_confidence ?? metrics.data_confidence;
  const gapMin = metrics.expected_gap_min;
  const gapMax = metrics.expected_gap_max;
  const isStable = presentation.status === "STABLE";
  const events = data.triggering_events || [];

  return (
    <>
      <PageIntro
        eyebrow="Nearest risk"
        title={
          isStable
            ? "가까운 결제는 현재 안전해요."
            : "가장 먼저 막아야 할 위험이에요."
        }
        description="전체 13주 중 가장 가까운 위험 하나를 먼저 보여드립니다. 계좌 배치 문제인지 실제 자금 부족인지도 나누어 설명해요."
        action={<StatusPill status={presentation.status} label={presentation.status_label} />}
      />

      <section className={`risk-hero ${isStable ? "stable" : ""}`}>
        <div className="risk-hero-main">
          <div className="risk-hero-date">
            <span>
              <Icon name={isStable ? "shield" : "calendar"} size={28} />
            </span>
            <div>
              <small>위험 예상일</small>
              <strong>
                {metrics.first_risk_date
                  ? formatDate(metrics.first_risk_date, true)
                  : isStable
                    ? "예정된 위험 없음"
                    : "날짜 정보 없음"}
              </strong>
              {typeof metrics.days_until_risk === "number" && (
                <p>{metrics.days_until_risk}일 남았어요</p>
              )}
            </div>
          </div>

          <div className="risk-hero-copy">
            <p className="eyebrow">{isStable ? "Safe window" : "Attention"}</p>
            <h3>
              {presentation.title ||
                (isStable ? "현재는 안전해요" : "위험 제목 정보 없음")}
            </h3>
            {presentation.impact && <p className="risk-hero-impact">{presentation.impact}</p>}
            {presentation.cause && <p className="risk-hero-cause">{presentation.cause}</p>}
          </div>

          {!isStable && (
            <Link className="button button-primary" href="/recommendations">
              부담이 적은 대응안 보기
              <Icon name="arrow" size={17} />
            </Link>
          )}
        </div>

        <aside className="risk-gap-panel">
          <p className="card-kicker">예상 부족금액</p>
          <strong>
            {typeof gapMin === "number" && typeof gapMax === "number"
              ? gapMin === gapMax
                ? formatWon(gapMin)
                : `${formatWon(gapMin)} – ${formatWon(gapMax)}`
              : "금액 범위 정보 없음"}
          </strong>
          <div className="risk-kind">
            <span>
              <Icon
                name={
                  metrics.shortfall_type === "PAYMENT_ACCOUNT"
                    ? "wallet"
                    : "risk"
                }
                size={19}
              />
            </span>
            <div>
              <small>부족 유형</small>
              <p>
                {metrics.shortfall_type === "PAYMENT_ACCOUNT"
                  ? "결제계좌만 부족할 수 있어요"
                  : metrics.shortfall_type === "TOTAL_LIQUIDITY"
                    ? "전체 자금이 부족할 수 있어요"
                    : isStable
                      ? "판정된 부족 유형 없음"
                      : "유형 정보 없음"}
              </p>
            </div>
          </div>
          <p className="risk-kind-help">
            {metrics.shortfall_type === "PAYMENT_ACCOUNT"
              ? "다른 가용계좌의 자금으로 부족분을 충당할 수 있는 상태입니다."
              : metrics.shortfall_type === "TOTAL_LIQUIDITY"
                ? "보호자금을 제외한 모든 가용자금을 모아도 부족할 수 있는 상태입니다."
                : "유형이 확인되면 계좌 이동으로 해결 가능한지 구분해 드립니다."}
          </p>
        </aside>
      </section>

      <section className="risk-facts card card-flat">
        <DataFact
          icon="shield"
          label="데이터 확인 진행도"
          value={formatPercent(confidence)}
          detail={confidenceProgressCopy(confidence)}
        />
        <DataFact
          icon="calendar"
          label="분석 기준"
          value={formatDateTime(data.as_of)}
          detail="Asia/Seoul 기준"
        />
        <DataFact
          icon="wallet"
          label="위험 구분"
          value={
            metrics.shortfall_type === "PAYMENT_ACCOUNT"
              ? "계좌 배치 문제"
              : metrics.shortfall_type === "TOTAL_LIQUIDITY"
                ? "전체 유동성 문제"
                : "정보 없음"
          }
          detail="보호자금은 가용자금에서 제외"
        />
      </section>

      <section>
        <SectionHeading
          eyebrow="Evidence"
          title="이 위험에 영향을 주는 항목"
          description="분석에 사용된 실제 이벤트와 원인을 숨기지 않고 보여드립니다."
        />
        {events.length || data.causes?.length ? (
          <div className="evidence-grid">
            {events.map((event, index) => {
              const date = recordString(event, "expected_date", "date", "occurred_at");
              const amount = recordNumber(event, "amount");
              return (
                <article className="evidence-card card card-flat" key={recordString(event, "event_id", "id") || index}>
                  <span className="evidence-icon">
                    <Icon
                      name={recordString(event, "direction") === "INFLOW" ? "receivables" : "calendar"}
                      size={20}
                    />
                  </span>
                  <div>
                    <span>{recordString(event, "event_type", "type") || "이벤트"}</span>
                    <strong>
                      {recordString(event, "description", "title", "counterparty_name") ||
                        "설명 정보 없음"}
                    </strong>
                    <p>
                      {date ? formatDate(date) : "날짜 정보 없음"}
                      {typeof amount === "number" ? ` · ${formatWon(amount)}` : ""}
                    </p>
                  </div>
                </article>
              );
            })}
            {data.causes?.map((cause, index) => (
              <article className="evidence-card card card-flat" key={`${cause}-${index}`}>
                <span className="evidence-icon cause">
                  <Icon name="info" size={20} />
                </span>
                <div>
                  <span>분석 원인</span>
                  <strong>{cause}</strong>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <EmptyState
            icon="info"
            title="연결된 이벤트 근거가 없어요"
            description="triggering_events 또는 causes가 API 응답에 포함되면 여기에 표시합니다."
          />
        )}
      </section>

      {presentation.recommended_action && (
        <section className="risk-next-action">
          <span>
            <Icon name="recommendations" size={25} />
          </span>
          <div>
            <p className="eyebrow">다음으로 할 일</p>
            <h3>{presentation.recommended_action}</h3>
            <p>실제 실행 전, 추천안 화면에서 13주 전후 효과를 가상으로 확인하세요.</p>
          </div>
          <Link className="button button-soft" href="/recommendations">
            추천안 검토
            <Icon name="arrow" size={17} />
          </Link>
        </section>
      )}
    </>
  );
}
