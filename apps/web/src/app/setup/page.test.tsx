import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import SetupPage from "./page";

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
  });
}

describe("SetupPage AI group split", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("posts once and replaces the grouped candidate with the server candidates", async () => {
    let splitRequestCount = 0;
    let resolveSplit: (response: Response) => void = () => undefined;
    const splitResponse = new Promise<Response>((resolve) => {
      resolveSplit = resolve;
    });
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const path = new URL(String(input)).pathname;
      if (path === "/api/v1/imports/transactions") {
        return Promise.resolve(
          jsonResponse({
            revision: "rev-1",
            analysis_required: true,
            imported_count: 4,
            candidates: [
              {
                candidate_id: "candidate-ai",
                candidate_type: "FIXED_EXPENSE",
                proposed_record: {
                  amount: 500_000,
                  description: "통합 카드사",
                },
                classification_group: {
                  source: "AI",
                  group_id: "group-1",
                  normalized_name: "통합 카드사",
                  entity_kind: "CARD_PAYMENT",
                  category_hint: "CARD_BILL",
                  essential_hint: true,
                  confidence: "HIGH",
                  reason: "같은 자동이체 항목입니다.",
                  labels: ["카드사 A", "카드사 B"],
                  can_split: true,
                },
              },
            ],
            classification_status: "SUCCEEDED",
            classification_summary: {
              source: "AI",
              applied: true,
              original_label_count: 2,
              grouped_entity_count: 1,
              merged_label_count: 1,
            },
          }),
        );
      }
      if (path === "/api/v1/candidates/candidate-ai/split") {
        expect(init?.method).toBe("POST");
        splitRequestCount += 1;
        return splitResponse;
      }
      return Promise.resolve(jsonResponse([]));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<SetupPage />);

    const file = new File(["transaction_id\ntransaction-1"], "track-a.csv", {
      type: "text/csv",
    });
    fireEvent.change(screen.getByTestId("setup-file-input"), {
      target: { files: [file] },
    });
    fireEvent.click(screen.getByTestId("setup-upload"));

    const splitButton = await screen.findByRole("button", {
      name: "그룹 해제",
    });
    fireEvent.click(splitButton);
    fireEvent.click(splitButton);
    expect(splitRequestCount).toBe(1);

    await act(async () => {
      resolveSplit(
        jsonResponse({
          split: true,
          candidate_id: "candidate-ai",
          candidates: [
            {
              candidate_id: "candidate-a",
              candidate_type: "FIXED_EXPENSE",
              proposed_record: { amount: 250_000, description: "개별 카드사 A" },
            },
            {
              candidate_id: "candidate-b",
              candidate_type: "FIXED_EXPENSE",
              proposed_record: { amount: 250_000, description: "개별 카드사 B" },
            },
          ],
          revision: "rev-2",
          analysis_required: true,
        }),
      );
    });

    await waitFor(() => {
      expect(screen.getByText("개별 카드사 A")).toBeTruthy();
      expect(screen.getByText("개별 카드사 B")).toBeTruthy();
    });
    expect(screen.queryByText("통합 카드사")).toBeNull();
    expect(screen.getByText("2건")).toBeTruthy();
    expect(screen.getByText(/사용자가 1개 그룹을 해제했습니다/)).toBeTruthy();
  });
});
