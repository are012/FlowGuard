"use client";

import { useEffect, useState } from "react";

import { Icon } from "@/components/icons";

interface AnalysisStage {
  key: string;
  label: string;
  description: string;
  seconds: number;
}

/**
 * SPECIFICATION.md의 `execution_stage` 순서를 그대로 따른다. 백엔드가 분석 도중
 * 단계를 알려주지 않으므로 경과 시간으로 추정만 하며, 화면에도 추정이라고 밝힌다.
 */
const ANALYSIS_STAGES: AnalysisStage[] = [
  {
    key: "SNAPSHOT_BUILDING",
    label: "금융 스냅숏 생성",
    description: "계좌·카드·할부·예정수입을 분석 시점 기준으로 고정합니다.",
    seconds: 2,
  },
  {
    key: "BASELINE_ANALYZING",
    label: "13주 현금흐름 계산",
    description: "날짜별 잔액과 Safe-to-Spend를 결정론적으로 계산합니다.",
    seconds: 4,
  },
  {
    key: "AGENT_INVESTIGATING",
    label: "위험 원인 조사",
    description: "거래처 지급이력과 금융 이벤트에서 위험 근거를 모읍니다.",
    seconds: 18,
  },
  {
    key: "PLAN_EVALUATING",
    label: "대응안 검증",
    description: "후보를 가상 적용해 반동위험과 금융정책 위반을 확인합니다.",
    seconds: 8,
  },
  {
    key: "REPORT_BUILDING",
    label: "리포트 생성",
    description: "검증을 통과한 결과만 사용자 화면용으로 정리합니다.",
    seconds: 3,
  },
];

const TOTAL_ESTIMATED_SECONDS = ANALYSIS_STAGES.reduce(
  (total, stage) => total + stage.seconds,
  0,
);

const SLOW_ANALYSIS_SECONDS = 35;

function activeStageIndex(elapsed: number) {
  let boundary = 0;
  for (let index = 0; index < ANALYSIS_STAGES.length; index += 1) {
    boundary += ANALYSIS_STAGES[index].seconds;
    if (elapsed < boundary) return index;
  }
  return ANALYSIS_STAGES.length - 1;
}

function formatElapsed(seconds: number) {
  if (seconds < 60) return `${seconds}초`;
  return `${Math.floor(seconds / 60)}분 ${String(seconds % 60).padStart(2, "0")}초`;
}

export function AnalysisProgress() {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);

  const currentIndex = activeStageIndex(elapsed);
  const isSlow = elapsed >= SLOW_ANALYSIS_SECONDS;
  const progress = Math.min(95, (elapsed / TOTAL_ESTIMATED_SECONDS) * 100);

  return (
    <section className="analysis-progress card" data-testid="analysis-progress">
      <div className="analysis-progress-head">
        <div>
          <p className="eyebrow">분석 진행 중</p>
          <h3>FlowGuard가 13주 유동성을 확인하고 있어요.</h3>
          <p>
            금융 코어가 숫자를 계산하고, 검증을 통과한 대응안만 추천으로 남깁니다.
          </p>
        </div>
        <div className="analysis-elapsed">
          <span className="analysis-spinner" aria-hidden="true" />
          <div>
            <small>경과 시간</small>
            <strong aria-hidden="true">{formatElapsed(elapsed)}</strong>
          </div>
        </div>
      </div>

      <div
        className="analysis-progress-bar"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(progress)}
        aria-label="분석 진행률(예상)"
      >
        <span style={{ width: `${progress}%` }} />
      </div>

      <ol className="analysis-stage-list">
        {ANALYSIS_STAGES.map((stage, index) => {
          const state =
            index < currentIndex
              ? "done"
              : index === currentIndex
                ? "active"
                : "pending";
          return (
            <li className={`analysis-stage stage-${state}`} key={stage.key}>
              <span className="stage-marker" aria-hidden="true">
                {state === "done" ? <Icon name="check" size={13} /> : index + 1}
              </span>
              <div>
                <strong>{stage.label}</strong>
                <p>{stage.description}</p>
              </div>
            </li>
          );
        })}
      </ol>

      <p aria-live="polite" className="analysis-stage-live">
        현재 예상 단계 · {ANALYSIS_STAGES[currentIndex].label}
      </p>

      {isSlow && (
        <div className="analysis-progress-slow">
          <Icon name="info" size={17} />
          <span>
            AI 해석이 예상보다 오래 걸리고 있어요. 응답이 없으면 결정론적 분석
            결과로 안전하게 대체하며, 금융 계산 결과는 그대로 유지됩니다.
          </span>
        </div>
      )}

      <p className="analysis-progress-note">
        단계 표시는 실제 분석 파이프라인의 순서를 경과 시간으로 추정한 것입니다.
        서버가 분석을 마치면 확정된 결과로 전환됩니다.
      </p>
    </section>
  );
}
