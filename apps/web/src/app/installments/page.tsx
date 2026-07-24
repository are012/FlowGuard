"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { Icon } from "@/components/icons";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageIntro,
  SectionHeading,
  StatusComparison,
  SubmitNotice,
} from "@/components/ui";
import { ApiError, apiRequest, useRemote } from "@/lib/api";
import {
  formatDate,
  formatWon,
} from "@/lib/format";
import type {
  Card,
  InstallmentPlan,
  InstallmentPrecheckResponse,
} from "@/lib/types";

type InstallmentsResponse =
  | InstallmentPlan[]
  | {
      items?: InstallmentPlan[];
      data?: InstallmentPlan[];
      installments?: InstallmentPlan[];
      installment_plans?: InstallmentPlan[];
    };

type CardsResponse =
  | Card[]
  | { items?: Card[]; data?: Card[]; cards?: Card[] };

function installmentsFrom(response?: InstallmentsResponse) {
  if (!response) return [];
  if (Array.isArray(response)) return response;
  return (
    response.installments ||
    response.installment_plans ||
    response.items ||
    response.data ||
    []
  );
}

function cardsFrom(response?: CardsResponse) {
  if (!response) return [];
  if (Array.isArray(response)) return response;
  return response.cards || response.items || response.data || [];
}

export default function InstallmentsPage() {
  const { data, error, loading, reload } =
    useRemote<InstallmentsResponse>("/api/v1/installments");
  const cardsRemote = useRemote<CardsResponse>("/api/v1/cards");
  const [purchaseAmount, setPurchaseAmount] = useState("");
  const [months, setMonths] = useState("6");
  const [firstPaymentDate, setFirstPaymentDate] = useState("");
  const [cardId, setCardId] = useState("");
  const [precheck, setPrecheck] = useState<InstallmentPrecheckResponse>();
  const [busy, setBusy] = useState(false);
  const [precheckError, setPrecheckError] = useState<string>();

  const installments = useMemo(() => installmentsFrom(data), [data]);
  const cards = useMemo(() => cardsFrom(cardsRemote.data), [cardsRemote.data]);
  const active = installments.filter(
    (item) => !item.status || item.status === "ACTIVE",
  );
  const remainingBurden = active.reduce((total, item) => {
    if (
      typeof item.monthly_payment !== "number" ||
      typeof item.remaining_months !== "number"
    ) {
      return total;
    }
    return total + item.monthly_payment * item.remaining_months;
  }, 0);
  const nextPaymentTotal = active.reduce(
    (total, item) => total + (item.monthly_payment || 0),
    0,
  );
  const endingPlan = active
    .filter((item) => item.remaining_months === 1)
    .length;
  const isInitial404 = error instanceof ApiError && error.status === 404 && !data;
  const evaluation = precheck?.evaluation;
  const policyViolations = precheck?.policy?.violations || [];

  async function runPrecheck(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      !purchaseAmount ||
      Number(purchaseAmount) <= 0 ||
      !months ||
      Number(months) <= 0 ||
      !firstPaymentDate ||
      !cardId
    ) {
      setPrecheckError("구매금액, 할부기간, 첫 결제일, 카드를 모두 입력해 주세요.");
      return;
    }

    setBusy(true);
    setPrecheck(undefined);
    setPrecheckError(undefined);
    try {
      const response = await apiRequest<InstallmentPrecheckResponse>(
        "/api/v1/installments/precheck",
        {
          method: "POST",
          body: JSON.stringify({
            purchase_amount: Number(purchaseAmount),
            installment_months: Number(months),
            first_payment_date: firstPaymentDate,
            card_id: cardId,
          }),
        },
      );
      setPrecheck(response);
    } catch (caught) {
      setPrecheckError(
        caught instanceof Error ? caught.message : "할부 영향을 분석하지 못했습니다.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (loading && !data) return <LoadingState label="남은 할부부담을 확인하고 있어요" />;
  if (error && !isInitial404 && !data) {
    return <ErrorState error={error} onRetry={reload} />;
  }

  return (
    <>
      <PageIntro
        eyebrow="Installment guard"
        title="지금의 구매가, 다음 달을 누르지 않도록."
        description="이미 약속된 할부는 미래의 필수 결제로 지키고, 새로운 구매는 13주 현금흐름에 미칠 영향을 먼저 확인합니다."
      />

      {installments.length ? (
        <>
          <section className="installment-summary">
            <article className="installment-total-card">
              <div>
                <p className="card-kicker light">남은 할부부담</p>
                <strong>{formatWon(remainingBurden)}</strong>
                <small>월 납부액 × 남은 회차 기준</small>
              </div>
              <span>
                <Icon name="installments" size={30} />
              </span>
            </article>
            <article className="summary-mini card card-flat">
              <span className="mini-icon">
                <Icon name="calendar" size={21} />
              </span>
              <div>
                <small>다음 회차 결제 합계</small>
                <strong>{formatWon(nextPaymentTotal)}</strong>
              </div>
            </article>
            <article className="summary-mini card card-flat">
              <span className="mini-icon">
                <Icon name="check" size={21} />
              </span>
              <div>
                <small>다음 회차 종료 예정</small>
                <strong>{endingPlan}건</strong>
              </div>
            </article>
          </section>

          <section>
            <SectionHeading
              eyebrow="Active plans"
              title="현재 진행 중인 할부"
              description="원거래 총액과 월별 납부액을 중복 반영하지 않고 실제 남은 회차만 보여드립니다."
            />
            <div className="installment-list">
              {installments.map((plan, index) => {
                const completed =
                  typeof plan.total_months === "number" &&
                  typeof plan.remaining_months === "number"
                    ? plan.total_months - plan.remaining_months
                    : undefined;
                const progress =
                  typeof completed === "number" &&
                  typeof plan.total_months === "number" &&
                  plan.total_months > 0
                    ? Math.max(0, Math.min(100, (completed / plan.total_months) * 100))
                    : undefined;
                return (
                  <article
                    className="installment-card card card-flat"
                    key={plan.installment_plan_id || index}
                  >
                    <div className="installment-card-top">
                      <span className="merchant-icon">
                        <Icon name="installments" size={20} />
                      </span>
                      <div>
                        <h3>
                          {plan.merchant_name ||
                            plan.description ||
                            plan.card_name ||
                            plan.card_id ||
                            "할부 설명 정보 없음"}
                        </h3>
                        <p>
                          {plan.total_months
                            ? `${plan.total_months}개월 할부`
                            : "전체 회차 정보 없음"}
                          {plan.card_name ? ` · ${plan.card_name}` : ""}
                        </p>
                      </div>
                      <span className={`plan-status ps-${plan.status?.toLowerCase() || "unknown"}`}>
                        {plan.status || "상태 정보 없음"}
                      </span>
                    </div>
                    <div className="installment-money">
                      <div>
                        <small>매월 결제</small>
                        <strong>
                          {typeof plan.monthly_payment === "number"
                            ? formatWon(plan.monthly_payment)
                            : "금액 정보 없음"}
                        </strong>
                      </div>
                      <div>
                        <small>남은 회차</small>
                        <strong>
                          {typeof plan.remaining_months === "number"
                            ? `${plan.remaining_months}회`
                            : "정보 없음"}
                        </strong>
                      </div>
                      <div>
                        <small>다음 결제일</small>
                        <strong>{formatDate(plan.next_payment_date)}</strong>
                      </div>
                    </div>
                    <div className="installment-progress">
                      <div>
                        <span
                          style={{
                            width:
                              typeof progress === "number" ? `${progress}%` : "0%",
                          }}
                        />
                      </div>
                      <small>
                        {typeof completed === "number" && plan.total_months
                          ? `${completed}/${plan.total_months}회 납부`
                          : "진행률 정보 없음"}
                      </small>
                    </div>
                  </article>
                );
              })}
            </div>
          </section>
        </>
      ) : (
        <EmptyState
          icon="installments"
          title={isInitial404 ? "아직 첫 분석이 없어요" : "진행 중인 할부가 없어요"}
          description={
            isInitial404
              ? "CSV를 연결하면 카드 할부 후보와 남은 회차를 찾아드려요."
              : "현재 API 응답에 등록된 할부 계획이 없습니다. 아래에서 새 구매의 영향은 미리 확인할 수 있어요."
          }
          action={
            isInitial404 ? (
              <Link className="button button-secondary" href="/setup">
                거래 CSV 연결
              </Link>
            ) : undefined
          }
        />
      )}

      <section className="precheck-section">
        <SectionHeading
          eyebrow="Before purchase"
          title="신규 할부 사전점검"
          description="클라이언트에서 임의 계산하지 않고 서버 금융 코어가 13주 전후 흐름과 안전정책을 다시 평가합니다."
        />
        <div className="precheck-layout">
          <form className="precheck-form card" onSubmit={runPrecheck}>
            <div className="precheck-form-heading">
              <span>
                <Icon name="plus" size={22} />
              </span>
              <div>
                <h3>구매 조건 입력</h3>
                <p>실제 할부 신청은 이루어지지 않습니다.</p>
              </div>
            </div>
            <div className="form-grid">
              <label className="field">
                <span>구매금액</span>
                <input
                  className="input"
                  inputMode="numeric"
                  min="1"
                  onChange={(event) => setPurchaseAmount(event.target.value)}
                  placeholder="예: 1500000"
                  required
                  type="number"
                  value={purchaseAmount}
                />
              </label>
              <label className="field">
                <span>할부기간</span>
                <select
                  className="select"
                  onChange={(event) => setMonths(event.target.value)}
                  value={months}
                >
                  {[2, 3, 6, 9, 12, 18, 24].map((value) => (
                    <option key={value} value={value}>
                      {value}개월
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>첫 결제일</span>
                <input
                  className="input"
                  onChange={(event) => setFirstPaymentDate(event.target.value)}
                  required
                  type="date"
                  value={firstPaymentDate}
                />
              </label>
              <label className="field">
                <span>결제 카드</span>
                <select
                  className="select"
                  onChange={(event) => setCardId(event.target.value)}
                  required
                  value={cardId}
                >
                  <option value="">카드 선택</option>
                  {cards.map((card, index) => (
                    <option key={card.card_id || index} value={card.card_id}>
                      {card.name || card.card_id || "이름 정보 없음"}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            {cardsRemote.error && (
              <SubmitNotice kind="error">
                카드 목록을 불러오지 못했습니다: {cardsRemote.error.message}
              </SubmitNotice>
            )}
            {!cardsRemote.loading && !cardsRemote.error && !cards.length && (
              <SubmitNotice kind="info">
                점검에 사용할 카드가 없습니다. 먼저 거래 CSV를 연결해 주세요.
              </SubmitNotice>
            )}
            {precheckError && <SubmitNotice kind="error">{precheckError}</SubmitNotice>}
            <button
              className="button button-primary button-wide"
              disabled={busy || !cards.length}
              type="submit"
            >
              {busy ? "13주 영향을 계산하는 중..." : "전체 유동성 영향 확인"}
              {!busy && <Icon name="arrow" size={17} />}
            </button>
          </form>

          <div className="precheck-result card card-flat" aria-live="polite">
            {!precheck ? (
              <div className="precheck-placeholder">
                <span>
                  <Icon name="cashflow" size={28} />
                </span>
                <h3>조건을 입력하면 결과가 여기에 표시돼요</h3>
                <p>
                  적용 전후 예상 부족금액, 이후 13주의 반동위험과 정책 위반을
                  서버 응답 그대로 보여드립니다.
                </p>
              </div>
            ) : (
              <div className="precheck-content">
                <div className="precheck-result-heading">
                  <div>
                    <p className="eyebrow">Simulation result</p>
                    <h3>
                      {evaluation?.valid === false ||
                      precheck.policy?.valid === false
                        ? "이 조건은 안전정책을 통과하지 못했어요"
                        : "가상 점검 결과가 준비됐어요"}
                    </h3>
                  </div>
                  <span
                    className={
                      evaluation?.valid === false ||
                      precheck.policy?.valid === false
                        ? "invalid"
                        : "valid"
                    }
                  >
                    {evaluation?.valid === false ||
                    precheck.policy?.valid === false
                      ? "조정 필요"
                      : "평가 완료"}
                  </span>
                </div>
                {evaluation?.before?.status || evaluation?.after?.status ? (
                  <StatusComparison
                    before={evaluation.before?.status}
                    after={evaluation.after?.status}
                  />
                ) : null}
                <div className="safe-change">
                  <div>
                    <span>구매 전 최대 예상 부족</span>
                    <strong>
                      {typeof evaluation?.before?.risk_metrics
                        ?.expected_gap_max === "number"
                        ? formatWon(
                            evaluation.before.risk_metrics.expected_gap_max,
                          )
                        : "정보 없음"}
                    </strong>
                  </div>
                  <Icon name="arrow" />
                  <div>
                    <span>구매 후 최대 예상 부족</span>
                    <strong>
                      {typeof evaluation?.after?.risk_metrics
                        ?.expected_gap_max === "number"
                        ? formatWon(
                            evaluation.after.risk_metrics.expected_gap_max,
                          )
                        : "정보 없음"}
                    </strong>
                  </div>
                </div>
                {evaluation?.risk_shift?.detected ? (
                  <div className="rebound-warning">
                    <Icon name="risk" size={21} />
                    <div>
                      <strong>이 구매는 이후 위험을 키울 수 있어요</strong>
                      <p>
                        {evaluation.risk_shift.message ||
                          `${formatDate(
                            evaluation.risk_shift.target_date,
                          )}에 새로운 위험이 감지됐습니다.`}
                      </p>
                    </div>
                  </div>
                ) : (
                  <div className="rebound-clear">
                    <Icon name="check" size={19} />
                    <div>
                      <strong>반동위험이 감지되지 않았어요</strong>
                      <p>서버가 반환한 평가 결과 기준입니다.</p>
                    </div>
                  </div>
                )}
                {[
                  ...(evaluation?.policy_violations || []),
                  ...policyViolations,
                ].map((violation, index) => (
                    <SubmitNotice kind="error" key={violation.code || index}>
                      {violation.message || violation.code || "정책 위반 정보"}
                    </SubmitNotice>
                  ))}
                <p className="precheck-version">
                  {evaluation?.tool_version
                    ? `평가 도구 ${evaluation.tool_version}`
                    : "평가 도구 버전 정보 없음"}
                </p>
              </div>
            )}
          </div>
        </div>
      </section>
    </>
  );
}
