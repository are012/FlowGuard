"use client";

import { useMemo, useRef, useState } from "react";

import { AnalysisProgress } from "@/components/analysis-progress";
import { Icon } from "@/components/icons";
import { PageIntro } from "@/components/ui";
import { apiRequest, listFrom, useRemote, waitForAnalysis } from "@/lib/api";
import type {
  Account,
  AnalysisResponse,
  Card,
  CandidateSplitResponse,
  Counterparty,
  DemoResetResponse,
  ImportCandidate,
  ImportResponse,
  SetupCommitResponse,
} from "@/lib/types";
import {
  CandidateReviewStep,
  CsvUploadStep,
  SetupCompleteStep,
  SetupProgressSidebar,
} from "./components";
import {
  candidateKey,
  candidatesFrom,
  newCounterpartyId,
  promotionFields,
  proposedValue,
  replaceCandidate,
  SAMPLE_ANALYSIS_AS_OF,
  SAMPLE_FILE_NAME,
  valueAsString,
  type CandidateDetails,
  type ListResponse,
  type ReviewValue,
} from "./model";

export default function SetupPage() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File>();
  const [incomeType, setIncomeType] = useState("MIXED");
  const [minimumReserve, setMinimumReserve] = useState("");
  const [importResult, setImportResult] = useState<ImportResponse>();
  const [candidates, setCandidates] = useState<ImportCandidate[]>([]);
  const [reviews, setReviews] = useState<Record<string, ReviewValue>>({});
  const [candidateDetails, setCandidateDetails] = useState<
    Record<string, CandidateDetails>
  >({});
  const [analysisResult, setAnalysisResult] = useState<AnalysisResponse>();
  const [busy, setBusy] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [splitErrors, setSplitErrors] = useState<Record<string, string>>({});
  const [splittingCandidates, setSplittingCandidates] = useState<
    Record<string, boolean>
  >({});
  const splitInFlightRef = useRef(new Set<string>());
  const accountsRemote =
    useRemote<ListResponse<Account>>("/api/v1/accounts");
  const cardsRemote = useRemote<ListResponse<Card>>("/api/v1/cards");
  const counterpartiesRemote =
    useRemote<ListResponse<Counterparty>>("/api/v1/counterparties");

  const splitBusy = Object.keys(splittingCandidates).length > 0;
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
    setCandidates([]);
    setAnalysisResult(undefined);
    setReviews({});
    setCandidateDetails({});
    setSplitErrors({});

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
      const importedCandidates = candidatesFrom(response);
      setImportResult(response);
      setCandidates(importedCandidates);
      if (response.classification_summary) {
        setReviews(
          Object.fromEntries(
            importedCandidates.flatMap((candidate, index) =>
              candidate.status && candidate.status !== "PENDING"
                ? [[candidateKey(candidate, index), candidate.status]]
                : [],
            ),
          ),
        );
      }
      accountsRemote.reload();
      cardsRemote.reload();
      counterpartiesRemote.reload();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "CSV 업로드에 실패했습니다.");
    } finally {
      setBusy(false);
    }
  }

  async function splitCandidate(candidate: ImportCandidate, key: string) {
    const candidateId = candidate.candidate_id || candidate.id;
    if (!candidateId || splitInFlightRef.current.has(candidateId)) return;

    splitInFlightRef.current.add(candidateId);
    setSplittingCandidates((current) => ({ ...current, [candidateId]: true }));
    setSplitErrors((current) => {
      const next = { ...current };
      delete next[candidateId];
      return next;
    });

    try {
      const response = await apiRequest<CandidateSplitResponse>(
        `/api/v1/candidates/${encodeURIComponent(candidateId)}/split`,
        { method: "POST" },
      );
      if (
        !response.split ||
        response.candidate_id !== candidateId ||
        !Array.isArray(response.candidates)
      ) {
        throw new Error("그룹 해제 결과를 확인하지 못했습니다.");
      }

      setCandidates((current) =>
        replaceCandidate(current, candidateId, response.candidates),
      );
      setImportResult((current) =>
        current
          ? {
              ...current,
              revision: response.revision,
              analysis_required: response.analysis_required,
              classification_summary: current.classification_summary
                ? {
                    ...current.classification_summary,
                    user_split_group_count:
                      (current.classification_summary.user_split_group_count ||
                        0) + 1,
                  }
                : undefined,
            }
          : current,
      );
      setReviews((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
      setCandidateDetails((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
    } catch (caught) {
      setSplitErrors((current) => ({
        ...current,
        [candidateId]:
          caught instanceof Error
            ? caught.message
            : "그룹을 해제하지 못했습니다.",
      }));
    } finally {
      splitInFlightRef.current.delete(candidateId);
      setSplittingCandidates((current) => {
        const next = { ...current };
        delete next[candidateId];
        return next;
      });
    }
  }

  async function startAnalysis() {
    if (!importResult || splitInFlightRef.current.size > 0) return;
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
        // 서버가 비동기 모드면 202 와 함께 QUEUED 만 돌아온다.
        // 그때는 대시보드 상태로 완료를 기다린다.
        if (response.analysis_status === "QUEUED" && !response.analysis_id) {
          const finished = await waitForAnalysis<AnalysisResponse>({
            onProgress: (snapshot) => setAnalysisResult(snapshot),
          });
          setAnalysisResult(finished ?? response);
        } else {
          setAnalysisResult(response);
        }
      } else {
        setAnalysisResult({
          status: "COMPLETED",
          analysis_status: "SUCCEEDED",
          interpretation_status: "NOT_REQUESTED",
          execution_stage: "COMPLETED",
          refresh_status: "SUCCEEDED",
          revision: committed.revision,
          analysis_revision: committed.revision,
          report_revision: committed.revision,
          latest_data_revision: committed.revision,
          is_stale: false,
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
      setCandidates([]);
      setAnalysisResult(undefined);
      setReviews({});
      setCandidateDetails({});
      setSplitErrors({});
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
            disabled={busy || splitBusy}
            onClick={resetDemo}
            type="button"
          >
            <Icon name="refresh" size={17} />
            샘플 데이터 초기화
          </button>
        }
      />

      <div className="setup-layout">
        <SetupProgressSidebar
          hasAnalysis={Boolean(analysisResult)}
          hasImport={Boolean(importResult)}
        />

        <div className="setup-main">
          {!importResult && (
            <CsvUploadStep
              busy={busy || splitBusy}
              error={error}
              file={file}
              fileInputRef={fileInputRef}
              incomeType={incomeType}
              minimumReserve={minimumReserve}
              notice={notice}
              onFileChange={setFile}
              onIncomeTypeChange={setIncomeType}
              onMinimumReserveChange={setMinimumReserve}
              onSubmit={uploadCsv}
            />
          )}

          {importResult && !analysisResult && (
            <CandidateReviewStep
              accounts={accounts}
              accountsError={accountsRemote.error}
              accountsLoading={accountsRemote.loading}
              analyzing={analyzing}
              busy={busy}
              candidateDetails={candidateDetails}
              candidates={candidates}
              cards={cards}
              cardsError={cardsRemote.error}
              cardsLoading={cardsRemote.loading}
              counterparties={counterparties}
              counterpartiesError={counterpartiesRemote.error}
              counterpartiesLoading={counterpartiesRemote.loading}
              error={error}
              importResult={importResult}
              onAnalyze={startAnalysis}
              onReset={resetDemo}
              onReview={(key, value) =>
                setReviews((current) => ({ ...current, [key]: value }))
              }
              onUpdateCandidateDetail={updateCandidateDetail}
              onSplit={splitCandidate}
              resolveCandidateDetails={resolvedCandidateDetails}
              reviews={reviews}
              splitBusy={splitBusy}
              splitErrors={splitErrors}
              splittingCandidates={splittingCandidates}
            />
          )}

          {analyzing && (
            <AnalysisProgress executionStage={analysisResult?.execution_stage} />
          )}

          {analysisResult && (
            <SetupCompleteStep
              analysisResult={analysisResult}
              importResult={importResult}
            />
          )}
        </div>
      </div>
    </>
  );
}
