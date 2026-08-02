import type { ImportCandidate, ImportResponse } from "@/lib/types";

export type ReviewValue = "CONFIRMED" | "REJECTED" | "UNKNOWN";
export type CandidateDetails = Record<string, string>;
export type ListResponse<T> = T[] | { items?: T[] } | { data?: T[] };

export const SAMPLE_FILE_NAME = "flowguard-synthetic-transactions.csv";
export const SAMPLE_ANALYSIS_AS_OF = "2026-07-24T09:00:00+09:00";

export const promotionFields: Record<
  string,
  Array<{ key: string; label: string; kind: "text" | "number" | "boolean" }>
> = {
  RECURRING_INCOME: [
    { key: "counterparty_id", label: "거래처", kind: "text" },
    { key: "amount", label: "예정 금액", kind: "number" },
    { key: "expected_date", label: "입금 예정일", kind: "text" },
    { key: "destination_account_id", label: "입금 받을 계좌", kind: "text" },
  ],
  FIXED_EXPENSE: [
    { key: "amount", label: "예정 금액", kind: "number" },
    { key: "expected_date", label: "출금 예정일", kind: "text" },
    { key: "account_id", label: "출금 계좌", kind: "text" },
    { key: "event_type", label: "지출 종류", kind: "text" },
    { key: "is_essential", label: "필수 지출 여부", kind: "boolean" },
  ],
  INSTALLMENT: [
    { key: "card_id", label: "결제 카드", kind: "text" },
    { key: "original_amount", label: "원 결제금액", kind: "number" },
    { key: "total_months", label: "전체 할부 개월", kind: "number" },
    { key: "next_payment_date", label: "다음 결제일", kind: "text" },
  ],
};

export function proposedValue(candidate: ImportCandidate, field: string) {
  return candidate.proposed_record?.[field];
}

export function valueAsString(value: unknown) {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return "";
}

export function newCounterpartyId(candidate: ImportCandidate, key: string) {
  const source = candidate.candidate_id || candidate.id || key;
  return `candidate-counterparty-${source}`.slice(0, 100);
}

export function candidateKey(candidate: ImportCandidate, index: number) {
  return (
    candidate.candidate_id ||
    candidate.transaction_id ||
    candidate.id ||
    `candidate-${index}`
  );
}

export function candidatesFrom(response: ImportResponse) {
  const groups = [
    response.candidates,
    response.detected_candidates,
    response.recurring_income_candidates?.map((item) => ({
      ...item,
      candidate_type: item.candidate_type || "RECURRING_INCOME",
    })),
    response.fixed_expense_candidates?.map((item) => ({
      ...item,
      candidate_type: item.candidate_type || "FIXED_EXPENSE",
    })),
    response.installment_candidates?.map((item) => ({
      ...item,
      candidate_type: item.candidate_type || "INSTALLMENT",
    })),
  ];

  const seen = new Set<string>();
  return groups
    .flatMap((group) => group || [])
    .filter((candidate, index) => {
      const key = candidateKey(candidate, index);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}
