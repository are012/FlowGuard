"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { Icon } from "@/components/icons";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageIntro,
  StatusComparison,
  SubmitNotice,
} from "@/components/ui";
import { ApiError, apiRequest, useRemote } from "@/lib/api";
import {
  actionLabel,
  evaluationAfter,
  evaluationBefore,
  formatDate,
  formatWon,
  recommendationActions,
  recommendationId,
} from "@/lib/format";
import type {
  ActionDefinition,
  Recommendation,
  RecommendationComparisonCandidate,
} from "@/lib/types";

type RecommendationsResponse =
  | Recommendation[]
  | {
      items?: Recommendation[];
      data?: Recommendation[];
      recommendations?: Recommendation[];
      alternatives?: Recommendation[];
    };

function recommendationsFrom(response?: RecommendationsResponse) {
  if (!response) return [];
  if (Array.isArray(response)) return response;
  return (
    response.recommendations ||
    response.alternatives ||
    response.items ||
    response.data ||
    []
  );
}

function actionDetails(action: ReturnType<typeof recommendationActions>[number]) {
  const parameters = action.parameters || {};
  const entries: Array<{ label: string; value: string }> = [];
  if (typeof parameters.amount === "number") {
    entries.push({ label: "필요 금액", value: formatWon(parameters.amount) });
  }
  if (typeof parameters.purchase_amount === "number") {
    entries.push({ label: "구매 금액", value: formatWon(parameters.purchase_amount) });
  }
  if (typeof parameters.execution_date === "string") {
    entries.push({ label: "가상 실행일", value: formatDate(parameters.execution_date) });
  }
  if (typeof parameters.to_date === "string") {
    entries.push({ label: "변경 후 날짜", value: formatDate(parameters.to_date) });
  }
  if (typeof parameters.installment_months === "number") {
    entries.push({ label: "할부기간", value: `${parameters.installment_months}개월` });
  }
  return entries;
}

function friendlyAssumption(value: string) {
  return value
    .replace(
      "amount는 baseline_result.risk_metrics.expected_gap_max에서 가져왔습니다.",
      "현재 분석에서 가장 크게 예상되는 부족금액을 기준으로 했습니다.",
    )
    .replaceAll(
      "baseline_result.risk_metrics.expected_gap_max",
      "현재 분석의 최대 예상 부족금액",
    );
}

interface RecommendationView {
  key: string;
  recommendation: Recommendation;
  persisted: boolean;
  comparison?: RecommendationComparisonCandidate;
}

function comparisonAction(
  candidate: RecommendationComparisonCandidate,
): ActionDefinition | undefined {
  if (!candidate.type) return undefined;
  return {
    action_id: candidate.candidate_id,
    type: candidate.type.toLowerCase(),
    parameters:
      typeof candidate.amount === "number" ? { amount: candidate.amount } : {},
  };
}

function comparisonViews(items: Recommendation[]): RecommendationView[] {
  return items.flatMap((recommendation, recommendationIndex) => {
    const primaryKey =
      recommendationId(recommendation) || `recommendation-${recommendationIndex}`;
    const compactComparisons = (recommendation.comparison_candidates || [])
      .filter((candidate) => !candidate.selected)
      .map((candidate) => ({
        key: `comparison-${candidate.candidate_id}`,
        recommendation,
        comparison: candidate,
        persisted: false,
      }));
    const fallbackAlternatives =
      compactComparisons.length > 0
        ? []
        : (recommendation.alternatives || []).map((alternative, index) => ({
            key:
              recommendationId(alternative) ||
              `alternative-${primaryKey}-${index}`,
            recommendation: alternative,
            persisted: false,
          }));
    return [
      {
        key: primaryKey,
        recommendation,
        persisted: true,
      },
      ...compactComparisons,
      ...fallbackAlternatives,
    ];
  });
}

export default function RecommendationsPage() {
  const { data, error, loading, reload } =
    useRemote<RecommendationsResponse>("/api/v1/recommendations");
  const [selectedKey, setSelectedKey] = useState<string>();
  const [busyAction, setBusyAction] = useState<string>();
  const [actionError, setActionError] = useState<string>();
  const [actionSuccess, setActionSuccess] = useState<string>();

  const remoteItems = useMemo(() => recommendationsFrom(data), [data]);
  const views = useMemo(() => comparisonViews(remoteItems), [remoteItems]);
  const selectedView =
    views.find((view) => view.key === selectedKey) || views[0];
  const selected = selectedView?.recommendation;
  const selectedComparison = selectedView?.comparison;
  const currentId =
    selectedView?.persisted && selected ? recommendationId(selected) : undefined;

  async function act(type: "approve" | "reject") {
    if (!currentId || !selectedView?.persisted) {
      setActionError("비교 후보는 읽기 전용입니다. 우선 추천안을 선택해 주세요.");
      return;
    }
    setBusyAction(type);
    setActionError(undefined);
    setActionSuccess(undefined);
    try {
      await apiRequest<Recommendation>(
        `/api/v1/recommendations/${encodeURIComponent(currentId)}/${type}`,
        {
          method: "POST",
          body: JSON.stringify({}),
        },
      );

      setActionSuccess(
        type === "approve"
          ? "추천안을 가상 적용하도록 승인했습니다. 실제 금융거래는 실행되지 않습니다."
          : "이 추천안을 거절했습니다.",
      );
      reload();
    } catch (caught) {
      setActionError(
        caught instanceof Error ? caught.message : "추천안 요청을 처리하지 못했습니다.",
      );
    } finally {
      setBusyAction(undefined);
    }
  }

  async function showNextComparison() {
    const comparison = views.find((view) => !view.persisted);
    if (!comparison) {
      setActionSuccess("현재 데이터에서 비교 가능한 다른 후보가 없습니다.");
      return;
    }
    if (!currentId) {
      setActionError("우선 추천안 식별자가 없어 다른 방법을 요청할 수 없습니다.");
      return;
    }
    setBusyAction("alternatives");
    setActionError(undefined);
    setActionSuccess(undefined);
    try {
      await apiRequest<RecommendationsResponse>(
        `/api/v1/recommendations/${encodeURIComponent(currentId)}/alternatives`,
        {
          method: "POST",
          body: JSON.stringify({}),
        },
      );
      setSelectedKey(comparison.key);
      setActionSuccess("다른 후보의 가상 검증 결과를 보여드립니다.");
    } catch (caught) {
      setActionError(
        caught instanceof Error
          ? caught.message
          : "다른 방법 요청을 기록하지 못했습니다.",
      );
    } finally {
      setBusyAction(undefined);
    }
  }

  if (loading && !data) return <LoadingState label="부담이 적은 대응안을 확인하고 있어요" />;
  if (error instanceof ApiError && error.status === 404 && !data) {
    return (
      <EmptyState
        icon="recommendations"
        title="아직 만들어진 추천안이 없어요"
        description="첫 분석이 완료되면 정책 검증과 13주 재검증을 통과한 행동부터 보여드립니다."
        action={
          <Link className="button button-primary" href="/setup">
            첫 분석 시작
          </Link>
        }
      />
    );
  }
  if (error && !data) return <ErrorState error={error} onRetry={reload} />;
  if (!views.length || !selected || !selectedView) {
    return (
      <EmptyState
        icon="recommendations"
        title="현재 제안할 대응안이 없어요"
        description="안전한 상태이거나 분석 결과에 추천안이 아직 생성되지 않았습니다."
        action={
          <Link className="button button-secondary" href="/risk">
            위험 상태 확인
          </Link>
        }
      />
    );
  }

  const actions = selectedComparison
    ? [comparisonAction(selectedComparison)].filter(
        (action): action is ActionDefinition => Boolean(action),
      )
    : recommendationActions(selected);
  const primaryAction = actions[0];
  const before = evaluationBefore(selected);
  const after = selectedComparison
    ? {
        risk_metrics: {
          expected_gap_max: selectedComparison.after_expected_gap_max ?? undefined,
        },
      }
    : evaluationAfter(selected);
  const shift =
    selectedComparison?.risk_shift ||
    selected.evaluation?.risk_shift ||
    selected.risk_shift;
  const violations = selectedComparison
    ? selectedComparison.policy_violations
    : selected.evaluation?.policy_violations ||
      selected.policy_result?.violations ||
      selected.policy_violations ||
      [];
  const isValid = selectedComparison
    ? selectedComparison.feasible
    : selected.evaluation?.valid !== false &&
      selected.policy_result?.valid !== false &&
      !violations.length;
  const selectedTitle = selectedComparison
    ? primaryAction
      ? actionLabel(primaryAction)
      : selectedComparison.type || "비교 후보"
    : selected.title ||
      (primaryAction ? actionLabel(primaryAction) : "추천 행동 정보 없음");
  const selectedRationale = selectedComparison
    ? selectedComparison.rejection_reason ||
      (selectedComparison.feasible
        ? "금융 코어와 안전정책 검증을 통과한 비교 후보입니다."
        : "금융 코어 또는 안전정책 검증을 통과하지 못한 후보입니다.")
    : selected.rationale ||
      selected.summary ||
      selected.reason ||
      "현재 금융정보와 안전정책을 바탕으로 선택한 대응안입니다.";

  return (
    <>
      <PageIntro
        eyebrow="Recommended action"
        title="가장 부담이 적은 방법부터."
        description="현재 위험만 줄이는 것이 아니라, 이후 13주에 문제가 옮겨가지 않는지 금융 코어가 다시 계산한 결과입니다."
        action={
          <span className="recommendation-count">
            <strong>{views.length}</strong>
            <span>검토 가능한 방법</span>
          </span>
        }
      />

      <div className="recommendations-layout">
        <aside
          className="recommendation-selector card card-flat"
          data-testid="recommendation-comparison"
        >
          <p className="eyebrow">대응안 비교</p>
          <div>
            {views.map((view, index) => {
              const item = view.recommendation;
              const comparison = view.comparison;
              const action = comparison
                ? comparisonAction(comparison)
                : recommendationActions(item)[0];
              const itemAfter = comparison
                ? comparison.after_expected_gap_max
                : evaluationAfter(item)?.risk_metrics?.expected_gap_max;
              const itemViolations = comparison
                ? comparison.policy_violations
                : item.evaluation?.policy_violations ||
                  item.policy_result?.violations ||
                  item.policy_violations ||
                  [];
              const itemValid = comparison
                ? comparison.feasible
                : item.evaluation?.valid !== false &&
                  item.policy_result?.valid !== false &&
                  itemViolations.length === 0;
              const itemReason =
                comparison?.rejection_reason ||
                itemViolations[0]?.message ||
                itemViolations[0]?.code ||
                (comparison?.risk_shift?.detected
                  ? comparison.risk_shift.message || "다음 위험일로 문제가 이동합니다."
                  : null);
              const active = view === selectedView;
              return (
                <button
                  className={active ? "active" : ""}
                  data-candidate-id={comparison?.candidate_id || view.key}
                  key={view.key}
                  onClick={() => setSelectedKey(view.key)}
                  type="button"
                >
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <div>
                    <strong>
                      {comparison
                        ? action
                          ? actionLabel(action)
                          : comparison.type || "비교 후보"
                        : item.title ||
                          (action ? actionLabel(action) : "행동 정보 없음")}
                    </strong>
                    <small>{view.persisted ? "우선 추천" : "읽기 전용 비교"}</small>
                    <span className="candidate-comparison-meta">
                      <span className={itemValid ? "valid" : "invalid"}>
                        {itemValid ? "정책 통과" : "탈락"}
                      </span>
                      <span>
                        적용 후 부족{" "}
                        {typeof itemAfter === "number"
                          ? formatWon(itemAfter)
                          : "정보 없음"}
                      </span>
                    </span>
                    {itemReason && (
                      <small className="candidate-rejection-reason">
                        {itemReason}
                      </small>
                    )}
                  </div>
                  <Icon name="arrow" size={16} />
                </button>
              );
            })}
          </div>
        </aside>

        <div className="recommendation-detail">
          <section className="primary-plan card">
            <div className="primary-plan-top">
              <span className="plan-icon">
                <Icon name="recommendations" size={27} />
              </span>
              <span className={`policy-chip ${isValid ? "valid" : "invalid"}`}>
                <Icon name={isValid ? "shield" : "risk"} size={15} />
                {isValid ? "안전정책 확인" : "정책 확인 필요"}
              </span>
            </div>
            <p className="eyebrow">
              {selectedView.persisted ? "FlowGuard 우선 추천" : "비교 후보"}
            </p>
            <h2>{selectedTitle}</h2>
            <p className="plan-rationale">{selectedRationale}</p>

            {actions.length > 0 && (
              <div className="plan-actions">
                {actions.map((action, index) => (
                  <article key={action.action_id || `${action.type}-${index}`}>
                    <span className="action-order">{index + 1}</span>
                    <div>
                      <strong>{actionLabel(action)}</strong>
                      {action.assumptions?.length ? (
                        <p>
                          {action.assumptions
                            .map(friendlyAssumption)
                            .join(" · ")}
                        </p>
                      ) : null}
                      {actionDetails(action).length ? (
                        <dl>
                          {actionDetails(action).map((detail) => (
                            <div key={detail.label}>
                              <dt>{detail.label}</dt>
                              <dd>{detail.value}</dd>
                            </div>
                          ))}
                        </dl>
                      ) : null}
                    </div>
                  </article>
                ))}
              </div>
            )}

            {selectedView.persisted && selected.derivation && (
              <details
                className="recommendation-evidence"
                data-testid="recommendation-evidence"
              >
                <summary>왜 이 금액인가요?</summary>
                <p>
                  추천 금액 <strong>{formatWon(selected.derivation.amount)}</strong>은
                  {selected.derivation.source_field ===
                  "risk_metrics.expected_gap_max"
                    ? " 적용 전 최대 예상 부족액 "
                    : " 조정 대상 금액 "}
                  <strong>{formatWon(selected.derivation.source_value)}</strong>을
                  기준으로 산출했습니다.
                </p>
                <dl>
                  <div>
                    <dt>산출 기준</dt>
                    <dd>{selected.derivation.source_field}</dd>
                  </div>
                  <div>
                    <dt>계산식</dt>
                    <dd>{selected.derivation.formula}</dd>
                  </div>
                  <div>
                    <dt>근거 이벤트</dt>
                    <dd>
                      {selected.derivation.evidence_ids.length
                        ? selected.derivation.evidence_ids.join(", ")
                        : "연결된 이벤트 없음"}
                    </dd>
                  </div>
                  <div>
                    <dt>스냅숏 · revision</dt>
                    <dd>
                      {selected.derivation.snapshot_id} ·{" "}
                      {selected.derivation.revision}
                    </dd>
                  </div>
                  <div>
                    <dt>검증 버전</dt>
                    <dd>
                      {selected.derivation.tool_version || "도구 버전 정보 없음"} ·{" "}
                      {selected.derivation.policy_version}
                    </dd>
                  </div>
                </dl>
                <Link className="inline-link" href="/risk">
                  위험 이벤트 자세히 보기
                  <Icon name="arrow" size={16} />
                </Link>
              </details>
            )}
          </section>

          <section className="evaluation-card card card-flat">
            <div className="evaluation-heading">
              <div>
                <p className="eyebrow">13주 가상 검증</p>
                <h3>적용하면 어떻게 달라질까요?</h3>
              </div>
              <span className="virtual-label">실제 실행 아님</span>
            </div>
            {before?.status || after?.status ? (
              <StatusComparison before={before?.status} after={after?.status} />
            ) : null}
            <div className="safe-change">
              <div>
                <span>
                  {typeof before?.safe_to_spend === "number"
                    ? "적용 전 안심자금"
                    : "적용 전 최대 예상 부족"}
                </span>
                <strong>
                  {typeof before?.safe_to_spend === "number"
                    ? formatWon(before.safe_to_spend)
                    : typeof before?.risk_metrics?.expected_gap_max === "number"
                      ? formatWon(before.risk_metrics.expected_gap_max)
                    : "정보 없음"}
                </strong>
              </div>
              <Icon name="arrow" />
              <div>
                <span>
                  {typeof after?.safe_to_spend === "number"
                    ? "가상 적용 후 안심자금"
                    : "적용 후 최대 예상 부족"}
                </span>
                <strong>
                  {typeof after?.safe_to_spend === "number"
                    ? formatWon(after.safe_to_spend)
                    : typeof after?.risk_metrics?.expected_gap_max === "number"
                      ? formatWon(after.risk_metrics.expected_gap_max)
                    : "정보 없음"}
                </strong>
              </div>
            </div>

            {shift?.detected ? (
              <div className="rebound-warning">
                <Icon name="risk" size={21} />
                <div>
                  <strong>이 방법은 문제를 뒤로 미룰 수 있어요</strong>
                  <p>
                    {shift.message ||
                      `${formatDate(shift.source_date)}의 위험이 ${formatDate(
                        shift.target_date,
                      )}로 이동할 수 있습니다.`}
                  </p>
                </div>
              </div>
            ) : (
              <div className="rebound-clear">
                <Icon name="check" size={19} />
                <div>
                  <strong>반동위험이 감지되지 않았어요</strong>
                  <p>API가 반환한 13주 평가 결과 기준입니다.</p>
                </div>
              </div>
            )}

            {violations.length > 0 && (
              <div className="policy-violations">
                {violations.map((violation, index) => (
                  <p key={violation.code || index}>
                    <Icon name="risk" size={16} />
                    {violation.message || violation.code || "정책 위반 정보"}
                  </p>
                ))}
              </div>
            )}
            {selectedComparison?.rejection_reason && !violations.length && (
              <div className="policy-violations">
                <p>
                  <Icon name="risk" size={16} />
                  {selectedComparison.rejection_reason}
                </p>
              </div>
            )}
          </section>

          <section className="approval-card">
            <div>
              <Icon name="shield" size={21} />
              <p>
                승인은 이 행동을 금융 스냅숏에 <strong>가상 적용</strong>하는
                데만 사용됩니다. 실제 이체·결제일 변경·금융상품 신청은 하지 않습니다.
              </p>
            </div>
            {actionError && <SubmitNotice kind="error">{actionError}</SubmitNotice>}
            {actionSuccess && (
              <SubmitNotice kind="success">{actionSuccess}</SubmitNotice>
            )}
            {selectedView.persisted ? (
              <div className="approval-actions">
                <button
                  className="button button-danger"
                  disabled={Boolean(busyAction)}
                  onClick={() => act("reject")}
                  type="button"
                >
                  {busyAction === "reject" ? "거절하는 중..." : "이 추천 거절"}
                </button>
                <button
                  className="button button-secondary"
                  disabled={Boolean(busyAction)}
                  onClick={showNextComparison}
                  type="button"
                >
                  {busyAction === "alternatives" ? "찾는 중..." : "다른 방법 보기"}
                </button>
                <button
                  className="button button-primary"
                  data-testid="recommendation-approve"
                  disabled={
                    Boolean(busyAction) || !isValid || Boolean(shift?.detected)
                  }
                  onClick={() => act("approve")}
                  type="button"
                >
                  {busyAction === "approve" ? "승인하는 중..." : "가상 적용 승인"}
                  {busyAction !== "approve" && <Icon name="arrow" size={17} />}
                </button>
              </div>
            ) : (
              <div className="comparison-readonly">
                <SubmitNotice kind="info">
                  이 후보는 비교용 결과입니다. 승인·거절은 저장된 우선 추천안에서만
                  할 수 있습니다.
                </SubmitNotice>
                <button
                  className="button button-secondary"
                  onClick={() => setSelectedKey(views[0].key)}
                  type="button"
                >
                  우선 추천으로 돌아가기
                </button>
              </div>
            )}
          </section>
        </div>
      </div>
    </>
  );
}
