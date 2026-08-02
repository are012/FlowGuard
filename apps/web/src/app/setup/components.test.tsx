import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ImportCandidate, ImportResponse } from "@/lib/types";

import { CandidateReviewStep } from "./components";

const aiCandidate: ImportCandidate = {
  candidate_id: "candidate-ai",
  candidate_type: "FIXED_EXPENSE",
  proposed_record: {
    amount: 500_000,
    description: "카드사 자동이체",
    event_type: "OTHER_OUTFLOW",
    is_essential: true,
  },
  classification_group: {
    source: "AI",
    group_id: "group-1",
    normalized_name: "카드사 자동이체",
    entity_kind: "CARD_PAYMENT",
    category_hint: "CARD_BILL",
    essential_hint: true,
    confidence: "HIGH",
    reason: "같은 자동이체 항목입니다.",
    labels: ["카드사자동이체 월 표기", "카드사 자동이체"],
    can_split: true,
  },
};

function renderReview(
  importResult: ImportResponse,
  candidates: ImportCandidate[],
  changes: Partial<React.ComponentProps<typeof CandidateReviewStep>> = {},
) {
  const onSplit = vi.fn();
  render(
    <CandidateReviewStep
      accounts={[]}
      accountsLoading={false}
      analyzing={false}
      busy={false}
      candidateDetails={{}}
      candidates={candidates}
      cards={[]}
      cardsLoading={false}
      counterparties={[]}
      counterpartiesLoading={false}
      importResult={importResult}
      onAnalyze={vi.fn()}
      onReset={vi.fn()}
      onReview={vi.fn()}
      onSplit={onSplit}
      onUpdateCandidateDetail={vi.fn()}
      resolveCandidateDetails={() => ({})}
      reviews={{}}
      splitBusy={false}
      splitErrors={{}}
      splittingCandidates={{}}
      {...changes}
    />,
  );
  return { onSplit };
}

describe("CandidateReviewStep AI classification", () => {
  afterEach(cleanup);

  it("shows the AI source and lets the user split an AI group", () => {
    const { onSplit } = renderReview(
      {
        revision: "rev-1",
        analysis_required: true,
        classification_status: "SUCCEEDED",
        classification_summary: {
          source: "AI",
          applied: true,
          original_label_count: 5,
          grouped_entity_count: 3,
          merged_label_count: 2,
        },
      },
      [aiCandidate],
    );

    expect(
      screen.getByText("자동 탐지 출처: AI 분류 적용 · 분류 완료"),
    ).toBeTruthy();
    expect(screen.getByText("카드사자동이체 월 표기 · 카드사 자동이체")).toBeTruthy();
    expect(screen.getByText("AI 제안 · 카드 대금 · 필수 지출 예")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "그룹 해제" }));

    expect(onSplit).toHaveBeenCalledWith(aiCandidate, "candidate-ai");
  });

  it("lets the user review and change AI category details before confirmation", () => {
    const onUpdateCandidateDetail = vi.fn();
    renderReview(
      { revision: "rev-1", analysis_required: true },
      [aiCandidate],
      {
        onUpdateCandidateDetail,
        resolveCandidateDetails: () => ({
          event_type: "OTHER_OUTFLOW",
          is_essential: "true",
        }),
        reviews: { "candidate-ai": "CONFIRMED" },
      },
    );

    const category = screen.getByLabelText("지출 종류");
    const essential = screen.getByLabelText("필수 지출인가요?");
    expect(category).toHaveProperty("value", "OTHER_OUTFLOW");
    expect(essential).toHaveProperty("value", "true");

    fireEvent.change(category, { target: { value: "UTILITY" } });
    fireEvent.change(essential, { target: { value: "false" } });

    expect(onUpdateCandidateDetail).toHaveBeenCalledWith(
      "candidate-ai",
      "event_type",
      "UTILITY",
    );
    expect(onUpdateCandidateDetail).toHaveBeenCalledWith(
      "candidate-ai",
      "is_essential",
      "false",
    );
  });

  it("does not change the legacy review UI when classification fields are absent", () => {
    renderReview(
      { revision: "rev-1", analysis_required: true },
      [{ candidate_id: "legacy", candidate_type: "FIXED_EXPENSE" }],
    );

    expect(screen.queryByText(/자동 탐지 출처:/)).toBeNull();
    expect(screen.queryByRole("button", { name: "그룹 해제" })).toBeNull();
  });

  it("disables the split action while the request is in flight and shows its error", () => {
    renderReview(
      { revision: "rev-1", analysis_required: true },
      [aiCandidate],
      {
        splitBusy: true,
        splitErrors: { "candidate-ai": "그룹 해제에 실패했습니다." },
        splittingCandidates: { "candidate-ai": true },
      },
    );

    const button = screen.getByRole("button", {
      name: "그룹을 해제하는 중...",
    });
    expect(button).toHaveProperty("disabled", true);
    expect(screen.getByRole("alert").textContent).toContain(
      "그룹 해제에 실패했습니다.",
    );
  });

  it("explains why a single-label AI group cannot be split", () => {
    renderReview(
      { revision: "rev-1", analysis_required: true },
      [
        {
          ...aiCandidate,
          classification_group: {
            ...aiCandidate.classification_group!,
            can_split: false,
          },
        },
      ],
    );

    const button = screen.getByRole("button", { name: "그룹 해제" });
    const help = screen.getByText("이 그룹은 개별 후보로 나눌 수 없습니다.");
    expect(button).toHaveProperty("disabled", true);
    expect(button.getAttribute("aria-describedby")).toBe(help.id);
  });
});
