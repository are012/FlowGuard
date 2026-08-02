import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentTraceStep, DashboardResponse } from "@/lib/types";

import DashboardPage from "./page";

const useRemoteMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;

    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
  },
  useRemote: useRemoteMock,
}));

function dashboard(mode: "DETERMINISTIC" | "AI_INVESTIGATED" | "AI_PARTIAL") {
  const steps: AgentTraceStep[] = Array.from({ length: 3 }, (_, index) => {
    const legacyStep = {
      sequence: index + 1,
      kind: index === 1 ? "TOOL_CALL" : "CANDIDATE_EVALUATION",
      title: `단계 ${index + 1}`,
      summary: `요약 ${index + 1}`,
      status: "COMPLETED",
    };
    if (mode === "DETERMINISTIC" || index !== 1) return legacyStep;
    return {
      ...legacyStep,
      status: mode === "AI_PARTIAL" ? "AUDIT_ONLY" : "COMPLETED",
      source: "AI" as const,
      phase: 1,
      reason: "거래처 지급 이력을 확인할 필요가 있습니다.",
    };
  });
  if (mode === "AI_INVESTIGATED") {
    steps.push({
      sequence: 4,
      kind: "RISK_HYPOTHESIS",
      title: "AI 조사 가설",
      summary: "조사한 근거를 바탕으로 가설을 정리했습니다.",
      status: "COMPLETED",
      source: "AI",
      phase: 2,
      reason: "도구 조회 사유가 아닌 가설 설명입니다.",
    });
  }
  return {
    safe_to_spend: 100_000,
    presentation: { status: "STABLE", title: "가까운 결제 위험이 없어요" },
    analysis_status: "SUCCEEDED",
    interpretation_status: "FALLBACK",
    decision_trace: {
      mode,
      status: "COMPLETED",
      disclosure: "공개 감사 기록입니다.",
      steps,
      unresolved_questions:
        mode === "DETERMINISTIC" ? [] : ["추가 지급 근거는 확인하지 못했습니다."],
    },
  } satisfies DashboardResponse;
}

function renderDashboard(data: DashboardResponse) {
  useRemoteMock.mockReturnValue({
    data,
    loading: false,
    reload: vi.fn(),
  });
  return render(<DashboardPage />);
}

describe("DashboardPage investigation trace", () => {
  afterEach(() => {
    cleanup();
    useRemoteMock.mockReset();
  });

  it("keeps the legacy deterministic presentation unchanged", () => {
    const { container } = renderDashboard(dashboard("DETERMINISTIC"));

    expect(screen.getByText("규칙이 계산한 과정", { exact: true })).toBeTruthy();
    expect(
      screen.getByText("위험에서 추천까지 확인한 과정", { exact: true }),
    ).toBeTruthy();
    expect(screen.getByText("같은 정보 · 같은 결과", { exact: true })).toBeTruthy();
    expect(
      screen.getByText(
        "금액과 날짜, 위험 판정은 정해진 계산 규칙으로 구합니다. AI가 숫자를 만들어내지 않으므로, 같은 정보를 넣으면 언제나 같은 결과가 나옵니다. AI는 그 결과를 읽기 쉽게 설명하는 역할만 합니다.",
        { exact: true },
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "아래 단계는 규칙이 계산했고, AI는 그 결과를 이 문장으로 옮겨 적기만 합니다.",
        { exact: true },
      ),
    ).toBeTruthy();
    expect(container.querySelectorAll(".agent-trace-list > li")).toHaveLength(3);
    expect(container.querySelector(".trace-source")).toBeNull();
    expect(container.querySelector(".trace-reason")).toBeNull();
  });

  it("labels AI-selected lookups and keeps their reason collapsed", () => {
    const { container } = renderDashboard(dashboard("AI_INVESTIGATED"));

    expect(screen.getByText("AI와 규칙이 확인한 과정", { exact: true })).toBeTruthy();
    expect(screen.getByText("AI 조사 · 규칙 계산", { exact: true })).toBeTruthy();
    expect(screen.getByText("AI 조사 · 1차", { exact: true })).toBeTruthy();
    const details = container.querySelector(".trace-reason");
    expect(details).not.toBeNull();
    expect(details?.hasAttribute("open")).toBe(false);
    expect(
      screen.getByText("AI가 이 조회를 고른 이유", { exact: true }),
    ).toBeTruthy();
    expect(container.querySelectorAll(".trace-reason")).toHaveLength(1);
    expect(screen.getByText("확인하지 못한 것", { exact: true })).toBeTruthy();
  });

  it("states that a partial AI investigation was not applied to the final decision", () => {
    renderDashboard(dashboard("AI_PARTIAL"));

    expect(
      screen.getByText("AI 조사 일부 · 최종 판단 미적용", { exact: true }),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "아래 AI 조사 기록은 감사용으로만 남겼고 최종 판단에는 적용하지 않았습니다. 최종 결과는 규칙이 계산했습니다.",
        { exact: true },
      ),
    ).toBeTruthy();
    expect(screen.getByText("AI 조사 · 감사 전용", { exact: true })).toBeTruthy();
    expect(screen.getByText("감사 기록", { exact: true })).toBeTruthy();
  });
});
