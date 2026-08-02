"use client";

import Link from "next/link";
import { useMemo, useRef, useState } from "react";

import { AnalysisProgress } from "@/components/analysis-progress";
import { Icon } from "@/components/icons";
import { PageIntro, SubmitNotice } from "@/components/ui";
import { apiRequest, listFrom, useRemote } from "@/lib/api";
import {
  candidateTypeLabel,
  formatDate,
  formatPercent,
  formatWon,
} from "@/lib/format";
import type {
  Account,
  AnalysisResponse,
  Card,
  Counterparty,
  DemoResetResponse,
  ImportCandidate,
  ImportResponse,
  SetupCommitResponse,
} from "@/lib/types";

type ReviewValue = "CONFIRMED" | "REJECTED" | "UNKNOWN";
type CandidateDetails = Record<string, string>;
type ListResponse<T> = T[] | { items?: T[] } | { data?: T[] };

const SAMPLE_FILE_NAME = "flowguard-synthetic-transactions.csv";
const SAMPLE_ANALYSIS_AS_OF = "2026-07-24T09:00:00+09:00";

const promotionFields: Record<
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

function proposedValue(candidate: ImportCandidate, field: string) {
  return candidate.proposed_record?.[field];
}

function valueAsString(value: unknown) {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return "";
}

function newCounterpartyId(candidate: ImportCandidate, key: string) {
  const source = candidate.candidate_id || candidate.id || key;
  return `candidate-counterparty-${source}`.slice(0, 100);
}

function candidateKey(candidate: ImportCandidate, index: number) {
  return (
    candidate.candidate_id ||
    candidate.transaction_id ||
    candidate.id ||
    `candidate-${index}`
  );
}

function candidatesFrom(response: ImportResponse) {
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

export default function SetupPage() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File>();
  const [incomeType, setIncomeType] = useState("MIXED");
  const [minimumReserve, setMinimumReserve] = useState("");
  const [importResult, setImportResult] = useState<ImportResponse>();
  const [reviews, setReviews] = useState<Record<string, ReviewValue>>({});
  const [candidateDetails, setCandidateDetails] = useState<
    Record<string, CandidateDetails>
  >({});
  const [analysisResult, setAnalysisResult] = useState<AnalysisResponse>();
  const [busy, setBusy] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const accountsRemote =
    useRemote<ListResponse<Account>>("/api/v1/accounts");
  const cardsRemote = useRemote<ListResponse<Card>>("/api/v1/cards");
  const counterpartiesRemote =
    useRemote<ListResponse<Counterparty>>("/api/v1/counterparties");

  const candidates = useMemo(
    () => (importResult ? candidatesFrom(importResult) : []),
    [importResult],
  );
  const accounts = useMemo(
    () => listFrom<Account>(accountsRemote.data),
    [accountsRemote.data],
  );
  const cards = useMemo(
    () => listFrom<Card>(cardsRemote.data),
    [cardsRemote.data],
  );
  const counterparties = useMemo(
    () => listFrom<Counterparty>(counterpartiesRemote.data),
    [counterpartiesRemote.data],
  );
  const reviewCount = Object.keys(reviews).length;

  function updateCandidateDetail(
    key: string,
    field: string,
    value: string,
  ) {
    setCandidateDetails((current) => ({
      ...current,
      [key]: { ...current[key], [field]: value },
    }));
  }

  function resolvedCandidateDetails(
    candidate: ImportCandidate,
    key: string,
  ): CandidateDetails {
    const edits = candidateDetails[key] || {};
    const proposedName = valueAsString(
      proposedValue(candidate, "counterparty_name"),
    );
    const matchingCounterparty = counterparties.find(
      (counterparty) => counterparty.name === proposedName,
    );

    function resolve(field: string, fallback = "") {
      if (edits[field] !== undefined) return edits[field];
      return valueAsString(proposedValue(candidate, field)) || fallback;
    }

    const counterpartyId = resolve(
      "counterparty_id",
      matchingCounterparty?.counterparty_id ||
        (proposedName ? newCounterpartyId(candidate, key) : ""),
    );
    const cardId = resolve("card_id");
    const matchingCard = cards.find((card) => card.card_id === cardId);

    return {
      amount: resolve("amount", valueAsString(candidate.amount)),
      original_amount: resolve(
        "original_amount",
        valueAsString(candidate.amount),
      ),
      total_months: resolve("total_months"),
      card_id: cardId,
      next_payment_date: resolve(
        "next_payment_date",
        matchingCard?.billing_date || "",
      ),
      counterparty_id: counterpartyId,
      expected_date: resolve("expected_date"),
      destination_account_id: resolve("destination_account_id"),
      account_id: resolve("account_id"),
      event_type: resolve("event_type"),
      is_essential: resolve("is_essential"),
    };
  }

  function confirmationDetails(
    candidate: ImportCandidate,
    key: string,
  ): { details: Record<string, unknown>; missing: string[] } {
    const type = candidate.candidate_type || candidate.type || "";
    const values = resolvedCandidateDetails(candidate, key);
    const details: Record<string, unknown> = {};
    const missing: string[] = [];

    for (const field of promotionFields[type] || []) {
      const value = values[field.key];
      if (
        value === "" ||
        (field.kind === "number" &&
          (!Number.isFinite(Number(value)) || Number(value) <= 0))
      ) {
        missing.push(field.label);
        continue;
      }

      const original = proposedValue(candidate, field.key);
      const wasEdited = candidateDetails[key]?.[field.key] !== undefined;
      if (original === undefined || original === null || wasEdited) {
        details[field.key] =
          field.kind === "number"
            ? Number(value)
            : field.kind === "boolean"
              ? value === "true"
              : value;
      }
    }

    return { details, missing };
  }

  async function uploadCsv(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file) {
      setError("업로드할 CSV 파일을 먼저 선택해 주세요.");
      return;
    }
    if (minimumReserve && Number(minimumReserve) < 0) {
      setError("최소로 남길 돈은 0원 이상이어야 합니다.");
      return;
    }

    setBusy(true);
    setError(undefined);
    setNotice(undefined);
    setImportResult(undefined);
    setAnalysisResult(undefined);
    setReviews({});
    setCandidateDetails({});

    const formData = new FormData();
    formData.append("file", file);
    if (file.name === SAMPLE_FILE_NAME) {
      formData.append("demo_mode", "true");
    }

    try {
      const response = await apiRequest<ImportResponse>(
        "/api/v1/imports/transactions",
        { method: "POST", body: formData },
      );
      setImportResult(response);
      accountsRemote.reload();
      cardsRemote.reload();
      counterpartiesRemote.reload();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "CSV 업로드에 실패했습니다.");
    } finally {
      setBusy(false);
    }
  }

  async function startAnalysis() {
    if (!importResult) return;
    const confirmations = candidates.map((candidate, index) => {
      const key = candidateKey(candidate, index);
      const decision = reviews[key] || "UNKNOWN";
      const confirmation =
        decision === "CONFIRMED"
          ? confirmationDetails(candidate, key)
          : { details: {}, missing: [] };
      return {
        candidate_id: candidate.candidate_id || candidate.id || null,
        decision,
        details: confirmation.details,
        missing: confirmation.missing,
      };
    });
    const incomplete = confirmations.find(
      (confirmation) =>
        confirmation.decision === "CONFIRMED" &&
        confirmation.missing.length > 0,
    );
    if (incomplete) {
      setError(
        `확정한 후보의 ${incomplete.missing.join(", ")} 정보를 입력해 주세요.`,
      );
      return;
    }

    setBusy(true);
    setAnalyzing(true);
    setError(undefined);
    setNotice(undefined);

    try {
      const committed = await apiRequest<SetupCommitResponse>(
        "/api/v1/setup/commit",
        {
          method: "POST",
          body: JSON.stringify({
            preferences: {
              income_type: incomeType,
              minimum_total_reserve: minimumReserve ? Number(minimumReserve) : 0,
            },
            candidates: confirmations
              .filter(
                (
                  confirmation,
                ): confirmation is typeof confirmation & { candidate_id: string } =>
                  Boolean(confirmation.candidate_id),
              )
              .map((confirmation) => ({
                candidate_id: confirmation.candidate_id,
                decision: confirmation.decision,
                ...(confirmation.decision === "CONFIRMED" &&
                Object.keys(confirmation.details).length
                  ? { details: confirmation.details }
                  : {}),
              })),
          }),
        },
      );

      if (committed.analysis_required) {
        const analysisAsOf =
          importResult.analysis_as_of ||
          (importResult.is_demo ? SAMPLE_ANALYSIS_AS_OF : undefined);
        const response = await apiRequest<AnalysisResponse>("/api/v1/analyses", {
          method: "POST",
          body: JSON.stringify({
            trigger_type: "DATA_REFRESH",
            ...(analysisAsOf ? { as_of: analysisAsOf } : {}),
          }),
        });
        setAnalysisResult(response);
      } else {
        setAnalysisResult({
          status: "COMPLETED",
          analysis_status: "COMPLETED",
          revision: committed.revision,
          analysis_revision: committed.revision,
          analysis_required: false,
        });
      }
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "확인 내용을 저장하거나 분석을 시작하지 못했습니다.",
      );
    } finally {
      setBusy(false);
      setAnalyzing(false);
    }
  }

  async function resetDemo() {
    if (
      !window.confirm(
        "현재 사용자의 샘플 금융정보와 분석 결과를 초기화할까요? 실제 금융정보에는 영향을 주지 않습니다.",
      )
    ) {
      return;
    }

    setBusy(true);
    setError(undefined);
    setNotice(undefined);
    try {
      const response = await apiRequest<DemoResetResponse>("/api/v1/demo/reset", {
        method: "POST",
        body: JSON.stringify({
          confirmation: "RESET_DEMO",
        }),
      });
      if (!response.reset || response.analysis_required) {
        throw new Error("샘플 데이터 초기화 상태를 확인하지 못했습니다.");
      }
      setFile(undefined);
      if (fileInputRef.current) fileInputRef.current.value = "";
      setImportResult(undefined);
      setAnalysisResult(undefined);
      setReviews({});
      setCandidateDetails({});
      accountsRemote.reload();
      cardsRemote.reload();
      counterpartiesRemote.reload();
      setNotice("샘플 데이터를 초기화했습니다. 합성 CSV를 다시 연결해 시작하세요.");
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "샘플 데이터를 초기화하지 못했습니다.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageIntro
        eyebrow="2분 안에 시작하기"
        title="거래내역 한 번이면 충분해요."
        description="합성 거래 CSV를 연결하면 반복 수입, 고정지출, 할부 후보를 찾아드립니다. 실제 계좌나 카드에는 연결하지 않습니다."
        action={
          <button
            className="button button-secondary"
            data-testid="demo-reset"
            disabled={busy}
            onClick={resetDemo}
            type="button"
          >
            <Icon name="refresh" size={17} />
            샘플 데이터 초기화
          </button>
        }
      />

      <div className="setup-layout">
        <aside className="setup-steps card card-flat" aria-label="설정 단계">
          <p className="eyebrow">Setup guide</p>
          <ol>
            <li className={!importResult ? "active" : "done"}>
              <span>{importResult ? <Icon name="check" size={15} /> : "1"}</span>
              <div>
                <strong>CSV 연결</strong>
                <small>거래내역 가져오기</small>
              </div>
            </li>
            <li
              className={
                analysisResult ? "done" : importResult ? "active" : ""
              }
            >
              <span>{analysisResult ? <Icon name="check" size={15} /> : "2"}</span>
              <div>
                <strong>자동 탐지 확인</strong>
                <small>예 · 아니요 · 잘 모르겠어요</small>
              </div>
            </li>
            <li className={analysisResult ? "active" : ""}>
              <span>3</span>
              <div>
                <strong>첫 분석</strong>
                <small>오늘의 안전자금 확인</small>
              </div>
            </li>
          </ol>
          <div className="setup-protection">
            <Icon name="shield" />
            <p>
              업로드한 데이터는 연결된 API로만 전송되며, 이 화면은 임의의 분석
              결과를 만들지 않습니다.
            </p>
          </div>
        </aside>

        <div className="setup-main">
          {!importResult && (
            <section className="setup-card card">
              <div className="setup-card-heading">
                <span className="setup-number">01</span>
                <div>
                  <h3>합성 거래내역을 준비해 주세요</h3>
                  <p>
                    아래 샘플 형식을 이용하거나 같은 컬럼의 합성 CSV를 올려주세요.
                  </p>
                </div>
              </div>

              <a
                className="sample-download"
                download
                href="/samples/flowguard-synthetic-transactions.csv"
              >
                <span className="sample-icon">
                  <Icon name="download" size={23} />
                </span>
                <span>
                  <strong>FlowGuard 합성 CSV 받기</strong>
                  <small>
                    6개월 수입·고정지출·카드·할부가 포함된 샘플
                  </small>
                </span>
                <span className="file-tag">CSV</span>
              </a>

              <form onSubmit={uploadCsv}>
                <button
                  className={`upload-zone ${file ? "has-file" : ""}`}
                  onClick={() => fileInputRef.current?.click()}
                  type="button"
                >
                  <input
                    accept=".csv,text/csv"
                    className="sr-only"
                    data-testid="setup-file-input"
                    onChange={(event) => setFile(event.target.files?.[0])}
                    ref={fileInputRef}
                    type="file"
                  />
                  <span className="upload-icon">
                    <Icon name={file ? "check" : "upload"} size={25} />
                  </span>
                  <strong>
                    {file ? file.name : "CSV 파일을 선택해 주세요"}
                  </strong>
                  <small>
                    {file
                      ? `${(file.size / 1024).toFixed(1)} KB · 업로드 준비 완료`
                      : "파일을 이 영역에서 선택할 수 있어요"}
                  </small>
                </button>

                <div className="form-grid setup-options">
                  <label className="field">
                    <span>주요 수입 형태</span>
                    <select
                      className="select"
                      onChange={(event) => setIncomeType(event.target.value)}
                      value={incomeType}
                    >
                      <option value="PROJECT">프로젝트 대금</option>
                      <option value="PLATFORM">플랫폼 정산</option>
                      <option value="CREATOR">광고·협찬·출연료</option>
                      <option value="STORE">매장 매출</option>
                      <option value="MIXED">여러 수입 형태가 섞여 있음</option>
                    </select>
                  </label>
                  <label className="field">
                    <span>계좌에 최소로 남길 돈</span>
                    <input
                      className="input"
                      inputMode="numeric"
                      min="0"
                      onChange={(event) => setMinimumReserve(event.target.value)}
                      placeholder="예: 500000"
                      type="number"
                      value={minimumReserve}
                    />
                    <small>세금 예비비와 별도로 지킬 최소 안전잔액입니다.</small>
                  </label>
                </div>

                {error && <SubmitNotice kind="error">{error}</SubmitNotice>}
                {notice && <SubmitNotice kind="success">{notice}</SubmitNotice>}
                <div className="form-actions">
                  <button
                    className="button button-primary"
                    data-testid="setup-upload"
                    disabled={busy || !file}
                    type="submit"
                  >
                    {busy ? "거래를 확인하는 중..." : "업로드하고 자동 탐지"}
                    {!busy && <Icon name="arrow" size={17} />}
                  </button>
                </div>
              </form>
            </section>
          )}

          {importResult && !analysisResult && (
            <section className="setup-card card">
              <div className="setup-card-heading">
                <span className="setup-number">02</span>
                <div>
                  <h3>찾은 패턴이 맞는지 확인해 주세요</h3>
                  <p>
                    확인하지 않은 예정 수입은 확정된 돈으로 계산하지 않습니다.
                  </p>
                </div>
              </div>

              <div className="import-summary">
                <div>
                  <span>가져온 거래</span>
                  <strong>
                    {typeof importResult.imported_count === "number"
                      ? `${importResult.imported_count}건`
                      : importResult.imported_counts
                        ? `${Object.values(importResult.imported_counts).reduce(
                            (total, count) => total + count,
                            0,
                          )}건`
                      : "서버 응답 확인 필요"}
                  </strong>
                </div>
                <div>
                  <span>중복 제외</span>
                  <strong>
                    {typeof importResult.duplicate_count === "number"
                      ? `${importResult.duplicate_count}건`
                      : "정보 없음"}
                  </strong>
                </div>
                <div>
                  <span>자동 탐지 후보</span>
                  <strong>{candidates.length}건</strong>
                </div>
              </div>

              {importResult.is_demo && (
                <SubmitNotice kind="info">
                  합성 샘플은{" "}
                  <strong data-testid="sample-analysis-as-of">
                    {formatDate(
                      importResult.analysis_as_of || SAMPLE_ANALYSIS_AS_OF,
                      true,
                    )}
                  </strong>
                  을 기준으로 분석해 언제 시연해도 같은 결과를 보여줍니다.
                </SubmitNotice>
              )}

              {candidates.length ? (
                <div className="candidate-list">
                  {candidates.map((candidate, index) => {
                    const key = candidateKey(candidate, index);
                    const proposed = candidate.proposed_record || {};
                    const type =
                      candidate.candidate_type || candidate.type || "";
                    const values = resolvedCandidateDetails(candidate, key);
                    const needsField = (field: string) =>
                      proposed[field] === undefined ||
                      proposed[field] === null;
                    const proposedCounterpartyName = valueAsString(
                      proposed.counterparty_name,
                    );
                    const hasMatchingCounterparty = counterparties.some(
                      (counterparty) =>
                        counterparty.name === proposedCounterpartyName,
                    );
                    const generatedCounterpartyId = newCounterpartyId(
                      candidate,
                      key,
                    );
                    const optionLoading =
                      (type === "RECURRING_INCOME" &&
                        (accountsRemote.loading ||
                          counterpartiesRemote.loading)) ||
                      (type === "FIXED_EXPENSE" &&
                        accountsRemote.loading) ||
                      (type === "INSTALLMENT" && cardsRemote.loading);
                    const optionError =
                      type === "RECURRING_INCOME"
                        ? accountsRemote.error || counterpartiesRemote.error
                        : type === "FIXED_EXPENSE"
                          ? accountsRemote.error
                          : type === "INSTALLMENT"
                            ? cardsRemote.error
                            : undefined;
                    const proposedAmount =
                      candidate.amount ??
                      proposed.amount ??
                      proposed.original_amount;
                    const occurrenceCount =
                      candidate.occurrences ||
                      candidate.evidence_transaction_ids?.length;
                    return (
                      <article className="candidate-card" key={key}>
                        <div className="candidate-main">
                          <span className="candidate-type">
                            {candidateTypeLabel(
                              candidate.candidate_type || candidate.type,
                            )}
                          </span>
                          <h4>
                            {candidate.counterparty_name ||
                              proposed.counterparty_name ||
                              candidate.description ||
                              proposed.description ||
                              "설명 정보 없음"}
                          </h4>
                          <p>
                            {typeof proposedAmount === "number"
                              ? formatWon(proposedAmount)
                              : "금액 정보 없음"}
                            {occurrenceCount
                              ? ` · ${occurrenceCount}회 발견`
                              : ""}
                            {typeof candidate.confidence === "number"
                              ? ` · 탐지 신뢰도 ${formatPercent(candidate.confidence)}`
                              : ""}
                          </p>
                        </div>
                        <fieldset className="review-options">
                          <legend>이 탐지 결과가 맞나요?</legend>
                          {(
                            [
                              ["CONFIRMED", "예"],
                              ["REJECTED", "아니요"],
                              ["UNKNOWN", "잘 모르겠어요"],
                            ] as const
                          ).map(([value, label]) => (
                            <button
                              className={reviews[key] === value ? "selected" : ""}
                              key={value}
                              onClick={() =>
                                setReviews((current) => ({
                                  ...current,
                                  [key]: value,
                                }))
                              }
                              type="button"
                            >
                              {label}
                            </button>
                          ))}
                        </fieldset>
                        {reviews[key] === "CONFIRMED" && (
                          <div className="candidate-confirmation">
                            <div className="candidate-confirmation-heading">
                              <span>
                                <Icon name="check" size={14} />
                              </span>
                              <p>
                                <strong>확정에 필요한 정보만 알려주세요.</strong>
                                <small>
                                  금액처럼 이미 탐지된 정보는 그대로 사용합니다.
                                </small>
                              </p>
                            </div>

                            {optionLoading && (
                              <p className="candidate-option-status">
                                계좌·카드·거래처 정보를 불러오는 중입니다.
                              </p>
                            )}
                            {optionError && (
                              <p className="candidate-option-error">
                                선택 목록을 불러오지 못했습니다:{" "}
                                {optionError.message}
                              </p>
                            )}

                            <div className="candidate-detail-grid">
                              {type === "RECURRING_INCOME" && (
                                <>
                                  {needsField("counterparty_id") && (
                                    <label className="field">
                                      <span>거래처</span>
                                      <select
                                        className="select"
                                        disabled={counterpartiesRemote.loading}
                                        onChange={(event) =>
                                          updateCandidateDetail(
                                            key,
                                            "counterparty_id",
                                            event.target.value,
                                          )
                                        }
                                        value={values.counterparty_id}
                                      >
                                        <option value="">
                                          거래처를 선택해 주세요
                                        </option>
                                        {counterparties.map((counterparty) => (
                                          <option
                                            key={counterparty.counterparty_id}
                                            value={counterparty.counterparty_id}
                                          >
                                            {counterparty.name ||
                                              "이름 정보 없음"}
                                          </option>
                                        ))}
                                        {!hasMatchingCounterparty &&
                                          proposedCounterpartyName && (
                                            <option
                                              value={generatedCounterpartyId}
                                            >
                                              새 거래처로 등록 ·{" "}
                                              {proposedCounterpartyName}
                                            </option>
                                          )}
                                      </select>
                                    </label>
                                  )}
                                  {needsField("amount") && (
                                    <label className="field">
                                      <span>예정 금액</span>
                                      <input
                                        className="input"
                                        min="1"
                                        onChange={(event) =>
                                          updateCandidateDetail(
                                            key,
                                            "amount",
                                            event.target.value,
                                          )
                                        }
                                        type="number"
                                        value={values.amount}
                                      />
                                    </label>
                                  )}
                                  {needsField("expected_date") && (
                                    <label className="field">
                                      <span>입금 예정일</span>
                                      <input
                                        className="input"
                                        onChange={(event) =>
                                          updateCandidateDetail(
                                            key,
                                            "expected_date",
                                            event.target.value,
                                          )
                                        }
                                        type="date"
                                        value={values.expected_date}
                                      />
                                    </label>
                                  )}
                                  {needsField("destination_account_id") && (
                                    <label className="field">
                                      <span>입금 받을 계좌</span>
                                      <select
                                        className="select"
                                        disabled={accountsRemote.loading}
                                        onChange={(event) =>
                                          updateCandidateDetail(
                                            key,
                                            "destination_account_id",
                                            event.target.value,
                                          )
                                        }
                                        value={values.destination_account_id}
                                      >
                                        <option value="">
                                          계좌를 선택해 주세요
                                        </option>
                                        {accounts.map((account) => (
                                          <option
                                            key={account.account_id}
                                            value={account.account_id}
                                          >
                                            {account.name ||
                                              account.account_name ||
                                              "이름 정보 없음"}
                                          </option>
                                        ))}
                                      </select>
                                    </label>
                                  )}
                                </>
                              )}

                              {type === "FIXED_EXPENSE" && (
                                <>
                                  {needsField("amount") && (
                                    <label className="field">
                                      <span>예정 금액</span>
                                      <input
                                        className="input"
                                        min="1"
                                        onChange={(event) =>
                                          updateCandidateDetail(
                                            key,
                                            "amount",
                                            event.target.value,
                                          )
                                        }
                                        type="number"
                                        value={values.amount}
                                      />
                                    </label>
                                  )}
                                  {needsField("expected_date") && (
                                    <label className="field">
                                      <span>다음 출금일</span>
                                      <input
                                        className="input"
                                        onChange={(event) =>
                                          updateCandidateDetail(
                                            key,
                                            "expected_date",
                                            event.target.value,
                                          )
                                        }
                                        type="date"
                                        value={values.expected_date}
                                      />
                                    </label>
                                  )}
                                  {needsField("account_id") && (
                                    <label className="field">
                                      <span>출금 계좌</span>
                                      <select
                                        className="select"
                                        disabled={accountsRemote.loading}
                                        onChange={(event) =>
                                          updateCandidateDetail(
                                            key,
                                            "account_id",
                                            event.target.value,
                                          )
                                        }
                                        value={values.account_id}
                                      >
                                        <option value="">
                                          계좌를 선택해 주세요
                                        </option>
                                        {accounts.map((account) => (
                                          <option
                                            key={account.account_id}
                                            value={account.account_id}
                                          >
                                            {account.name ||
                                              account.account_name ||
                                              "이름 정보 없음"}
                                          </option>
                                        ))}
                                      </select>
                                    </label>
                                  )}
                                  {needsField("event_type") && (
                                    <label className="field">
                                      <span>지출 종류</span>
                                      <select
                                        className="select"
                                        onChange={(event) =>
                                          updateCandidateDetail(
                                            key,
                                            "event_type",
                                            event.target.value,
                                          )
                                        }
                                        value={values.event_type}
                                      >
                                        <option value="">
                                          종류를 선택해 주세요
                                        </option>
                                        <option value="RENT">임차료</option>
                                        <option value="INSURANCE">보험료</option>
                                        <option value="UTILITY">공과금</option>
                                        <option value="LOAN_PAYMENT">
                                          대출 상환
                                        </option>
                                        <option value="TAX">세금</option>
                                        <option value="SAVINGS">저축</option>
                                        <option value="DISCRETIONARY_EXPENSE">
                                          선택 지출
                                        </option>
                                        <option value="OTHER_OUTFLOW">
                                          기타 지출
                                        </option>
                                      </select>
                                    </label>
                                  )}
                                  {needsField("is_essential") && (
                                    <label className="field">
                                      <span>필수 지출인가요?</span>
                                      <select
                                        className="select"
                                        onChange={(event) =>
                                          updateCandidateDetail(
                                            key,
                                            "is_essential",
                                            event.target.value,
                                          )
                                        }
                                        value={values.is_essential}
                                      >
                                        <option value="">
                                          선택해 주세요
                                        </option>
                                        <option value="true">예, 필수예요</option>
                                        <option value="false">
                                          아니요, 조정 가능해요
                                        </option>
                                      </select>
                                    </label>
                                  )}
                                </>
                              )}

                              {type === "INSTALLMENT" && (
                                <>
                                  {needsField("card_id") && (
                                    <label className="field">
                                      <span>결제 카드</span>
                                      <select
                                        className="select"
                                        disabled={cardsRemote.loading}
                                        onChange={(event) =>
                                          updateCandidateDetail(
                                            key,
                                            "card_id",
                                            event.target.value,
                                          )
                                        }
                                        value={values.card_id}
                                      >
                                        <option value="">
                                          카드를 선택해 주세요
                                        </option>
                                        {cards.map((card) => (
                                          <option
                                            key={card.card_id}
                                            value={card.card_id}
                                          >
                                            {card.name || "이름 정보 없음"}
                                          </option>
                                        ))}
                                      </select>
                                    </label>
                                  )}
                                  {needsField("original_amount") && (
                                    <label className="field">
                                      <span>원 결제금액</span>
                                      <input
                                        className="input"
                                        min="1"
                                        onChange={(event) =>
                                          updateCandidateDetail(
                                            key,
                                            "original_amount",
                                            event.target.value,
                                          )
                                        }
                                        type="number"
                                        value={values.original_amount}
                                      />
                                    </label>
                                  )}
                                  {needsField("total_months") && (
                                    <label className="field">
                                      <span>전체 할부 개월</span>
                                      <input
                                        className="input"
                                        min="1"
                                        onChange={(event) =>
                                          updateCandidateDetail(
                                            key,
                                            "total_months",
                                            event.target.value,
                                          )
                                        }
                                        type="number"
                                        value={values.total_months}
                                      />
                                    </label>
                                  )}
                                  {needsField("next_payment_date") && (
                                    <label className="field">
                                      <span>다음 결제일</span>
                                      <input
                                        className="input"
                                        onChange={(event) =>
                                          updateCandidateDetail(
                                            key,
                                            "next_payment_date",
                                            event.target.value,
                                          )
                                        }
                                        type="date"
                                        value={values.next_payment_date}
                                      />
                                      {values.next_payment_date &&
                                        !candidateDetails[key]
                                          ?.next_payment_date && (
                                          <small>
                                            카드의 다음 결제일을 불러왔습니다.
                                          </small>
                                        )}
                                    </label>
                                  )}
                                </>
                              )}
                            </div>
                          </div>
                        )}
                      </article>
                    );
                  })}
                </div>
              ) : (
                <div className="no-candidates">
                  <Icon name="info" />
                  <p>
                    서버가 자동 탐지 후보를 반환하지 않았습니다. 가져온 거래로 바로
                    분석을 시작할 수 있습니다.
                  </p>
                </div>
              )}

              {error && <SubmitNotice kind="error">{error}</SubmitNotice>}
              <div className="review-footer">
                <p>
                  {candidates.length
                    ? `${reviewCount}/${candidates.length}건 직접 확인 · 나머지는 잘 모르겠어요로 처리`
                    : "확인할 후보 없음"}
                </p>
                <div>
                  <button
                    className="button button-ghost"
                    disabled={busy}
                    onClick={resetDemo}
                    type="button"
                  >
                    서버 데이터 초기화 후 다시 선택
                  </button>
                  <button
                    className="button button-primary"
                    data-testid="setup-analyze"
                    disabled={busy}
                    onClick={startAnalysis}
                    type="button"
                  >
                    {analyzing
                      ? "분석하는 중..."
                      : busy
                        ? "처리하는 중..."
                        : "확인 저장하고 분석 시작"}
                    {!busy && <Icon name="arrow" size={17} />}
                  </button>
                </div>
              </div>
            </section>
          )}

          {analyzing && <AnalysisProgress />}

          {analysisResult && (
            <section className="setup-complete card">
              <span className="complete-icon">
                <Icon name="check" size={31} />
              </span>
              <p className="eyebrow">Setup complete</p>
              <h3>첫 분석을 요청했어요.</h3>
              <p>
                서버가 금융 스냅숏을 만들고 13주 현금흐름을 분석합니다. 완료 전에는
                임의의 금액을 표시하지 않습니다.
              </p>
              {importResult?.is_demo && (
                <SubmitNotice kind="info">
                  샘플 시나리오 기준일은{" "}
                  {formatDate(
                    importResult.analysis_as_of || SAMPLE_ANALYSIS_AS_OF,
                    true,
                  )}
                  입니다.
                </SubmitNotice>
              )}
              <div className="analysis-request-info">
                <span>분석 ID</span>
                <strong>
                  {analysisResult.analysis_run_id ||
                    analysisResult.analysis_id ||
                    "응답에 ID가 없습니다"}
                </strong>
                <span>현재 상태</span>
                <strong>
                  {analysisResult.analysis_status ||
                    analysisResult.status ||
                    "상태 정보 없음"}
                </strong>
              </div>
              <Link className="button button-primary" href="/">
                대시보드에서 결과 확인
                <Icon name="arrow" size={17} />
              </Link>
            </section>
          )}
        </div>
      </div>
    </>
  );
}
