"use client";

import Link from "next/link";

import { Icon } from "@/components/icons";
import {
  DataFact,
  EmptyState,
  ErrorState,
  InlineLink,
  LoadingState,
  PageIntro,
  StatusPill,
  SubmitNotice,
} from "@/components/ui";
import { ApiError, useRemote } from "@/lib/api";
import {
  actionLabel,
  formatDate,
  formatDateTime,
  formatPercent,
  formatWon,
  normalizeStatus,
  recommendationActions,
  statusLabel,
} from "@/lib/format";
import type { DashboardResponse, SafeToSpend } from "@/lib/types";

function safeToSpendValue(value?: SafeToSpend | number | null) {
  if (typeof value === "number") return value;
  return value?.safe_to_spend;
}

function safeToSpendDetails(value?: SafeToSpend | number | null) {
  return typeof value === "object" && value ? value : undefined;
}

function agentModeLabel(mode?: string) {
  if (mode === "LUNA") return "GPT-5.6 Luna";
  if (mode === "LUNA_NOT_REQUIRED") return "Luna · 대응 불필요";
  if (mode === "DETERMINISTIC_FALLBACK") return "결정론적 폴백";
  return "결정론적 조사";
}

function traceKindLabel(kind: string) {
  return (
    {
      RISK_HYPOTHESIS: "위험 확인",
      TOOL_CALL: "근거 조회",
      CANDIDATE_EVALUATION: "대응안 검증",
      FINAL_SELECTION: "최종 선택",
    }[kind] || "판단 단계"
  );
}

export default function DashboardPage() {
  const { data, error, loading, reload } =
    useRemote<DashboardResponse>("/api/v1/dashboard");

  if (loading && !data) return <LoadingState label="오늘의 안전자금을 계산하고 있어요" />;
  if (error instanceof ApiError && error.status === 404 && !data) {
    return (
      <EmptyState
        icon="wallet"
        title="아직 첫 분석이 없어요"
        description="합성 거래 CSV를 연결하면 실제 API 분석 결과로 대시보드를 채워드려요."
        action={
          <Link className="button button-primary" href="/setup">
            처음 설정 시작
          </Link>
        }
      />
    );
  }
  if (error && !data) return <ErrorState error={error} onRetry={reload} />;
  if (!data) {
    return (
      <EmptyState
        icon="wallet"
        title="아직 분석 결과가 없어요"
        description="거래 CSV를 연결하고 첫 분석을 시작하면 오늘 안심하고 쓸 수 있는 돈을 보여드려요."
        action={
          <Link className="button button-primary" href="/setup">
            처음 설정 시작
          </Link>
        }
      />
    );
  }

  const safeAmount = safeToSpendValue(data.safe_to_spend);
  const safeDetails = safeToSpendDetails(data.safe_to_spend);
  const presentation = data.presentation || {};
  const metrics = data.risk_metrics || {};
  const status = normalizeStatus(presentation.status);
  const recommendation = data.recommendation || undefined;
  const action = recommendationActions(recommendation || {})[0];
  const recommendationText =
    recommendation?.title ||
    (action ? actionLabel(action) : undefined) ||
    presentation.recommended_action;
  const confidence =
    safeDetails?.data_confidence ?? metrics.data_confidence;
  const updatedAt = data.last_successful_analysis_at || data.as_of;
  const bindingDate = safeDetails?.binding_constraint?.date;
  const missingCount = data.data_quality?.missing_sources?.length || 0;
  const staleCount = data.data_quality?.stale_sources?.length || 0;
  const unconfirmedCount = data.data_quality?.unconfirmed_items?.length || 0;
  const decisionTrace = data.decision_trace || undefined;

  if (
    typeof safeAmount !== "number" &&
    !presentation.title &&
    !recommendationText
  ) {
    return (
      <EmptyState
        icon="wallet"
        title="표시할 분석 결과가 없어요"
        description="분석은 생성되었지만 대시보드에 필요한 결과가 아직 준비되지 않았습니다."
        action={
          <button className="button button-secondary" onClick={reload} type="button">
            <Icon name="refresh" size={17} />
            새로고침
          </button>
        }
      />
    );
  }

  return (
    <>
      <PageIntro
        eyebrow="오늘의 금융 안전망"
        title="복잡한 흐름은 저희가 볼게요."
        description="수입 날짜가 달라져도 오늘의 생활과 다음 결제를 함께 지킬 수 있도록 정리했습니다."
        action={
          <Link className="button button-secondary" href="/setup">
            <Icon name="setup" size={18} />
            데이터 새로 연결
          </Link>
        }
      />

      {data.analysis_required && (
        <SubmitNotice kind="info">
          금융정보가 최근 분석 이후 변경되었습니다.{" "}
          <Link className="inline-link" href="/setup">
            데이터를 다시 확인하고 분석하기
          </Link>
        </SubmitNotice>
      )}

      <section className="dashboard-grid" aria-label="오늘의 핵심 분석">
        <article className="safe-card">
          <div className="safe-card-top">
            <div>
              <p className="card-kicker">오늘 안심하고 쓸 수 있는 돈</p>
              {typeof safeAmount === "number" ? (
                <p className="safe-amount">{formatWon(safeAmount)}</p>
              ) : (
                <p className="safe-amount unavailable">금액 정보 없음</p>
              )}
            </div>
            <span className="wallet-symbol">
              <Icon name="wallet" size={27} />
            </span>
          </div>
          <p className="safe-description">
            향후 13주의 필수지출, 보호자금, 최소 안전잔액을 반영한 금액입니다.
          </p>
          <div className="safe-card-footer">
            <span>
              {bindingDate
                ? `${formatDate(bindingDate)} 결제가 현재 한도를 결정해요`
                : "가장 가까운 제약 정보를 확인 중이에요"}
            </span>
          </div>
        </article>

        <article
          className={`risk-card risk-card-${status?.toLowerCase() || "unknown"}`}
        >
          <div className="card-row">
            <p className="card-kicker">가장 가까운 결제 위험</p>
            <StatusPill status={status} label={presentation.status_label} />
          </div>
          <div className="risk-date-row">
            <span className="risk-calendar">
              <Icon
                name={status === "STABLE" ? "shield" : "calendar"}
                size={23}
              />
            </span>
            <div>
              {metrics.first_risk_date && (
                <small>{formatDate(metrics.first_risk_date)}</small>
              )}
              <h3>
                {presentation.title ||
                  (status === "STABLE"
                    ? "가까운 결제 위험이 없어요"
                    : status
                      ? statusLabel[status]
                      : "위험 설명 정보 없음")}
              </h3>
            </div>
          </div>
          {presentation.impact && <p className="risk-impact">{presentation.impact}</p>}
          {presentation.cause && <p className="risk-cause">{presentation.cause}</p>}
          <InlineLink href="/risk">위험 근거 자세히 보기</InlineLink>
        </article>

        <article className="recommendation-card">
          <div className="recommendation-glow" aria-hidden="true" />
          <div className="card-row">
            <p className="card-kicker light">FlowGuard 우선 추천</p>
            <span className="recommendation-number">01</span>
          </div>
          <div className="recommendation-icon">
            <Icon name="recommendations" size={24} />
          </div>
          <h3>{recommendationText || "추천안을 준비하고 있어요"}</h3>
          <p>
            {recommendation?.rationale ||
              recommendation?.summary ||
              (recommendationText
                ? "현재 위험을 줄이면서 이후 13주에 새 위험을 만들지 않는지 확인한 행동입니다."
                : "분석이 완료되면 가장 부담이 적은 행동 하나를 먼저 보여드릴게요.")}
          </p>
          <Link className="recommendation-link" href="/recommendations">
            적용 전후 확인하기
            <span>
              <Icon name="arrow" size={18} />
            </span>
          </Link>
        </article>
      </section>

      {decisionTrace && (
        <section className="agent-trace" aria-labelledby="agent-trace-title">
          <div className="agent-trace-header">
            <div>
              <p className="card-kicker">검증 가능한 에이전트 판단</p>
              <h2 id="agent-trace-title">위험에서 추천까지 확인한 과정</h2>
              <p>
                {decisionTrace.mode === "LUNA"
                  ? "Luna가 선택한 근거와 금융 코어의 검증 결과만 공개합니다."
                  : "결정론적 조사기가 확인한 근거와 금융 코어의 검증 결과를 공개합니다."}{" "}
                숨겨진 모델 추론은 표시하지 않습니다.
              </p>
            </div>
            <div className="agent-mode">
              <span className={decisionTrace.mode === "LUNA" ? "is-luna" : ""}>
                {agentModeLabel(decisionTrace.mode)}
              </span>
              {decisionTrace.model && (
                <small>
                  {decisionTrace.mode === "LUNA"
                    ? decisionTrace.model
                    : `연결 대상 · ${decisionTrace.model}`}
                </small>
              )}
            </div>
          </div>

          {decisionTrace.fallback_reason && (
            <div className="agent-fallback">
              <Icon name="info" size={17} />
              <span>
                {decisionTrace.fallback_reason === "OPENAI_API_KEY_NOT_CONFIGURED"
                  ? "API 키가 없어 기존 결정론적 조사기로 안전하게 분석했습니다."
                  : "Luna 호출을 완료하지 못해 기존 결정론적 조사기로 안전하게 분석했습니다."}
              </span>
            </div>
          )}

          <ol className="agent-trace-list">
            {decisionTrace.steps.map((step) => (
              <li key={`${step.sequence}-${step.kind}-${step.candidate_id || ""}`}>
                <span className="trace-sequence">
                  {String(step.sequence).padStart(2, "0")}
                </span>
                <div>
                  <div className="trace-meta">
                    <span>{traceKindLabel(step.kind)}</span>
                    <small className={`trace-status status-${step.status.toLowerCase()}`}>
                      {step.status === "REJECTED"
                        ? "탈락"
                        : step.status === "NEEDS_REVIEW"
                          ? "검토 필요"
                          : step.status === "FAILED"
                            ? "확인 실패"
                            : "완료"}
                    </small>
                  </div>
                  <h3>{step.title}</h3>
                  <p>{step.summary}</p>
                  {(step.tool_name || step.candidate_id) && (
                    <code>{step.tool_name || step.candidate_id}</code>
                  )}
                </div>
              </li>
            ))}
          </ol>

          <p className="agent-disclosure">
            <Icon name="shield" size={16} />
            {decisionTrace.disclosure}
          </p>
        </section>
      )}

      <section className="analysis-strip" aria-label="분석 정보">
        <DataFact
          icon="shield"
          label="데이터 신뢰도"
          value={formatPercent(confidence)}
          detail={presentation.confidence_label}
        />
        <DataFact
          icon="refresh"
          label="최근 분석"
          value={formatDateTime(updatedAt)}
          detail={data.analysis_status ? `상태 · ${data.analysis_status}` : undefined}
        />
        <DataFact
          icon="info"
          label="확인 상태"
          value={
            unconfirmedCount
              ? `미확인 ${unconfirmedCount}건`
              : missingCount || staleCount
                ? "데이터 점검 필요"
                : "확인된 데이터"
          }
          detail={
            missingCount || staleCount
              ? `누락 ${missingCount} · 오래된 항목 ${staleCount}`
              : "현재 분석 기준"
          }
        />
        <Link className="analysis-more" href="/cashflow">
          <span>
            <small>더 멀리 보기</small>
            <strong>13주 현금흐름</strong>
          </span>
          <Icon name="arrow" />
        </Link>
      </section>

      <div className="safety-note">
        <Icon name="shield" size={19} />
        <p>
          FlowGuard는 실제 이체나 카드 설정 변경을 실행하지 않습니다. 추천 행동은
          사용자가 승인한 뒤에도 금융 스냅숏에만 가상 적용됩니다.
        </p>
      </div>
    </>
  );
}
