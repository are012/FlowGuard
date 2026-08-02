"use client";

import Link from "next/link";
import type { FormEventHandler, RefObject } from "react";

import { Icon } from "@/components/icons";
import { SubmitNotice } from "@/components/ui";
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
  ImportCandidate,
  ImportResponse,
  LabelCategoryHint,
} from "@/lib/types";

import {
  candidateKey,
  newCounterpartyId,
  SAMPLE_ANALYSIS_AS_OF,
  valueAsString,
  type CandidateDetails,
  type ReviewValue,
} from "./model";

interface SetupProgressSidebarProps {
  hasAnalysis: boolean;
  hasImport: boolean;
}

export function SetupProgressSidebar({
  hasAnalysis,
  hasImport,
}: SetupProgressSidebarProps) {
  return (
    <aside className="setup-steps card card-flat" aria-label="설정 단계">
      <p className="eyebrow">Setup guide</p>
      <ol>
        <li className={!hasImport ? "active" : "done"}>
          <span>{hasImport ? <Icon name="check" size={15} /> : "1"}</span>
          <div>
            <strong>CSV 연결</strong>
            <small>거래내역 가져오기</small>
          </div>
        </li>
        <li className={hasAnalysis ? "done" : hasImport ? "active" : ""}>
          <span>{hasAnalysis ? <Icon name="check" size={15} /> : "2"}</span>
          <div>
            <strong>자동 탐지 확인</strong>
            <small>예 · 아니요 · 잘 모르겠어요</small>
          </div>
        </li>
        <li className={hasAnalysis ? "active" : ""}>
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
          업로드한 데이터는 연결된 API로만 전송되며, 이 화면은 임의의 분석 결과를
          만들지 않습니다.
        </p>
      </div>
    </aside>
  );
}

interface CsvUploadStepProps {
  busy: boolean;
  error?: string;
  file?: File;
  fileInputRef: RefObject<HTMLInputElement | null>;
  incomeType: string;
  minimumReserve: string;
  notice?: string;
  onFileChange: (file?: File) => void;
  onIncomeTypeChange: (value: string) => void;
  onMinimumReserveChange: (value: string) => void;
  onSubmit: FormEventHandler<HTMLFormElement>;
}

export function CsvUploadStep({
  busy,
  error,
  file,
  fileInputRef,
  incomeType,
  minimumReserve,
  notice,
  onFileChange,
  onIncomeTypeChange,
  onMinimumReserveChange,
  onSubmit,
}: CsvUploadStepProps) {
  return (
    <section className="setup-card card">
      <div className="setup-card-heading">
        <span className="setup-number">01</span>
        <div>
          <h3>합성 거래내역을 준비해 주세요</h3>
          <p>아래 샘플 형식을 이용하거나 같은 컬럼의 합성 CSV를 올려주세요.</p>
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
          <small>6개월 수입·고정지출·카드·할부가 포함된 샘플</small>
        </span>
        <span className="file-tag">CSV</span>
      </a>

      <form onSubmit={onSubmit}>
        <button
          className={`upload-zone ${file ? "has-file" : ""}`}
          onClick={() => fileInputRef.current?.click()}
          type="button"
        >
          <input
            accept=".csv,text/csv"
            className="sr-only"
            data-testid="setup-file-input"
            onChange={(event) => onFileChange(event.target.files?.[0])}
            ref={fileInputRef}
            type="file"
          />
          <span className="upload-icon">
            <Icon name={file ? "check" : "upload"} size={25} />
          </span>
          <strong>{file ? file.name : "CSV 파일을 선택해 주세요"}</strong>
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
              onChange={(event) => onIncomeTypeChange(event.target.value)}
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
              onChange={(event) => onMinimumReserveChange(event.target.value)}
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
  );
}

interface CandidateOptions {
  accounts: Account[];
  accountsError?: Error;
  accountsLoading: boolean;
  cards: Card[];
  cardsError?: Error;
  cardsLoading: boolean;
  counterparties: Counterparty[];
  counterpartiesError?: Error;
  counterpartiesLoading: boolean;
}

const classificationCategoryLabels: Record<LabelCategoryHint, string> = {
  RECEIVABLE: "받을 돈",
  CARD_BILL: "카드 대금",
  INSTALLMENT_PAYMENT: "할부 결제",
  RENT: "임차료",
  INSURANCE: "보험료",
  UTILITY: "공과금",
  LOAN_PAYMENT: "대출 상환",
  TAX: "세금",
  SAVINGS: "저축",
  DISCRETIONARY_EXPENSE: "선택 지출",
  OTHER_INFLOW: "기타 수입",
  OTHER_OUTFLOW: "기타 지출",
};

interface CandidateCardProps extends CandidateOptions {
  candidate: ImportCandidate;
  candidateDetails: Record<string, CandidateDetails>;
  index: number;
  onReview: (key: string, value: ReviewValue) => void;
  onSplit: (candidate: ImportCandidate, key: string) => void;
  onUpdateCandidateDetail: (key: string, field: string, value: string) => void;
  resolveCandidateDetails: (
    candidate: ImportCandidate,
    key: string,
  ) => CandidateDetails;
  review?: ReviewValue;
  splitBusy: boolean;
  splitError?: string;
  splitting: boolean;
}

function CandidateCard({
  accounts,
  accountsError,
  accountsLoading,
  candidate,
  candidateDetails,
  cards,
  cardsError,
  cardsLoading,
  counterparties,
  counterpartiesError,
  counterpartiesLoading,
  index,
  onReview,
  onSplit,
  onUpdateCandidateDetail,
  resolveCandidateDetails,
  review,
  splitBusy,
  splitError,
  splitting,
}: CandidateCardProps) {
  const key = candidateKey(candidate, index);
  const proposed = candidate.proposed_record || {};
  const type = candidate.candidate_type || candidate.type || "";
  const values = resolveCandidateDetails(candidate, key);
  const needsField = (field: string) =>
    proposed[field] === undefined || proposed[field] === null;
  const proposedCounterpartyName = valueAsString(proposed.counterparty_name);
  const hasMatchingCounterparty = counterparties.some(
    (counterparty) => counterparty.name === proposedCounterpartyName,
  );
  const generatedCounterpartyId = newCounterpartyId(candidate, key);
  const optionLoading =
    (type === "RECURRING_INCOME" &&
      (accountsLoading || counterpartiesLoading)) ||
    (type === "FIXED_EXPENSE" && accountsLoading) ||
    (type === "INSTALLMENT" && cardsLoading);
  const optionError =
    type === "RECURRING_INCOME"
      ? accountsError || counterpartiesError
      : type === "FIXED_EXPENSE"
        ? accountsError
        : type === "INSTALLMENT"
          ? cardsError
          : undefined;
  const proposedAmount =
    candidate.amount ?? proposed.amount ?? proposed.original_amount;
  const occurrenceCount =
    candidate.occurrences || candidate.evidence_transaction_ids?.length;
  const classification = candidate.classification_group;
  const candidateId = candidate.candidate_id || candidate.id;
  const confidenceLabel =
    classification?.confidence === "HIGH"
      ? "높은 신뢰도"
      : classification?.confidence === "MEDIUM"
        ? "중간 신뢰도"
        : "낮은 신뢰도";

  return (
    <article className="candidate-card">
      <div className="candidate-main">
        <span className="candidate-type">
          {candidateTypeLabel(candidate.candidate_type || candidate.type)}
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
          {occurrenceCount ? ` · ${occurrenceCount}회 발견` : ""}
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
            className={review === value ? "selected" : ""}
            key={value}
            onClick={() => onReview(key, value)}
            type="button"
          >
            {label}
          </button>
        ))}
      </fieldset>
      {classification?.source === "AI" && (
        <div className="candidate-classification">
          <div className="candidate-classification-copy">
            <span>AI 분류 · {confidenceLabel}</span>
            <strong>{classification.normalized_name}</strong>
            <p>{classification.labels.join(" · ")}</p>
            <p>
              AI 제안 · {classificationCategoryLabels[classification.category_hint]}
              {" · "}
              필수 지출{" "}
              {classification.essential_hint === null
                ? "판단 보류"
                : classification.essential_hint
                  ? "예"
                  : "아니요"}
            </p>
            <small>{classification.reason}</small>
          </div>
          <button
            aria-busy={splitting}
            aria-describedby={
              classification.can_split
                ? undefined
                : `candidate-split-help-${candidateId}`
            }
            className="button button-secondary candidate-split-button"
            disabled={
              splitBusy || !candidateId || classification.can_split !== true
            }
            onClick={() => onSplit(candidate, key)}
            title={
              classification.can_split
                ? undefined
                : "이 그룹은 개별 후보로 나눌 수 없습니다."
            }
            type="button"
          >
            {splitting ? "그룹을 해제하는 중..." : "그룹 해제"}
          </button>
          {classification.can_split !== true && (
            <small id={`candidate-split-help-${candidateId}`}>
              이 그룹은 개별 후보로 나눌 수 없습니다.
            </small>
          )}
          {splitError && (
            <p className="candidate-split-error" role="alert">
              {splitError}
            </p>
          )}
        </div>
      )}
      {review === "CONFIRMED" && (
        <div className="candidate-confirmation">
          <div className="candidate-confirmation-heading">
            <span>
              <Icon name="check" size={14} />
            </span>
            <p>
              <strong>확정에 필요한 정보만 알려주세요.</strong>
              <small>금액처럼 이미 탐지된 정보는 그대로 사용합니다.</small>
            </p>
          </div>

          {optionLoading && (
            <p className="candidate-option-status">
              계좌·카드·거래처 정보를 불러오는 중입니다.
            </p>
          )}
          {optionError && (
            <p className="candidate-option-error">
              선택 목록을 불러오지 못했습니다: {optionError.message}
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
                      disabled={counterpartiesLoading}
                      onChange={(event) =>
                        onUpdateCandidateDetail(
                          key,
                          "counterparty_id",
                          event.target.value,
                        )
                      }
                      value={values.counterparty_id}
                    >
                      <option value="">거래처를 선택해 주세요</option>
                      {counterparties.map((counterparty) => (
                        <option
                          key={counterparty.counterparty_id}
                          value={counterparty.counterparty_id}
                        >
                          {counterparty.name || "이름 정보 없음"}
                        </option>
                      ))}
                      {!hasMatchingCounterparty && proposedCounterpartyName && (
                        <option value={generatedCounterpartyId}>
                          새 거래처로 등록 · {proposedCounterpartyName}
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
                        onUpdateCandidateDetail(key, "amount", event.target.value)
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
                        onUpdateCandidateDetail(
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
                      disabled={accountsLoading}
                      onChange={(event) =>
                        onUpdateCandidateDetail(
                          key,
                          "destination_account_id",
                          event.target.value,
                        )
                      }
                      value={values.destination_account_id}
                    >
                      <option value="">계좌를 선택해 주세요</option>
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
                        onUpdateCandidateDetail(key, "amount", event.target.value)
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
                        onUpdateCandidateDetail(
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
                      disabled={accountsLoading}
                      onChange={(event) =>
                        onUpdateCandidateDetail(
                          key,
                          "account_id",
                          event.target.value,
                        )
                      }
                      value={values.account_id}
                    >
                      <option value="">계좌를 선택해 주세요</option>
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
                {(needsField("event_type") || classification?.source === "AI") && (
                  <label className="field">
                    <span>지출 종류</span>
                    <select
                      className="select"
                      onChange={(event) =>
                        onUpdateCandidateDetail(
                          key,
                          "event_type",
                          event.target.value,
                        )
                      }
                      value={values.event_type}
                    >
                      <option value="">종류를 선택해 주세요</option>
                      <option value="RENT">임차료</option>
                      <option value="INSURANCE">보험료</option>
                      <option value="UTILITY">공과금</option>
                      <option value="LOAN_PAYMENT">대출 상환</option>
                      <option value="TAX">세금</option>
                      <option value="SAVINGS">저축</option>
                      <option value="DISCRETIONARY_EXPENSE">선택 지출</option>
                      <option value="OTHER_OUTFLOW">기타 지출</option>
                    </select>
                  </label>
                )}
                {(needsField("is_essential") || classification?.source === "AI") && (
                  <label className="field">
                    <span>필수 지출인가요?</span>
                    <select
                      className="select"
                      onChange={(event) =>
                        onUpdateCandidateDetail(
                          key,
                          "is_essential",
                          event.target.value,
                        )
                      }
                      value={values.is_essential}
                    >
                      <option value="">선택해 주세요</option>
                      <option value="true">예, 필수예요</option>
                      <option value="false">아니요, 조정 가능해요</option>
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
                      disabled={cardsLoading}
                      onChange={(event) =>
                        onUpdateCandidateDetail(key, "card_id", event.target.value)
                      }
                      value={values.card_id}
                    >
                      <option value="">카드를 선택해 주세요</option>
                      {cards.map((card) => (
                        <option key={card.card_id} value={card.card_id}>
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
                        onUpdateCandidateDetail(
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
                        onUpdateCandidateDetail(
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
                        onUpdateCandidateDetail(
                          key,
                          "next_payment_date",
                          event.target.value,
                        )
                      }
                      type="date"
                      value={values.next_payment_date}
                    />
                    {values.next_payment_date &&
                      !candidateDetails[key]?.next_payment_date && (
                        <small>카드의 다음 결제일을 불러왔습니다.</small>
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
}

interface CandidateReviewStepProps extends CandidateOptions {
  analyzing: boolean;
  busy: boolean;
  candidateDetails: Record<string, CandidateDetails>;
  candidates: ImportCandidate[];
  error?: string;
  importResult: ImportResponse;
  onAnalyze: () => void;
  onReset: () => void;
  onReview: (key: string, value: ReviewValue) => void;
  onSplit: (candidate: ImportCandidate, key: string) => void;
  onUpdateCandidateDetail: (key: string, field: string, value: string) => void;
  resolveCandidateDetails: (
    candidate: ImportCandidate,
    key: string,
  ) => CandidateDetails;
  reviews: Record<string, ReviewValue>;
  splitBusy: boolean;
  splitErrors: Record<string, string>;
  splittingCandidates: Record<string, boolean>;
}

export function CandidateReviewStep({
  accounts,
  accountsError,
  accountsLoading,
  analyzing,
  busy,
  candidateDetails,
  candidates,
  cards,
  cardsError,
  cardsLoading,
  counterparties,
  counterpartiesError,
  counterpartiesLoading,
  error,
  importResult,
  onAnalyze,
  onReset,
  onReview,
  onSplit,
  onUpdateCandidateDetail,
  resolveCandidateDetails,
  reviews,
  splitBusy,
  splitErrors,
  splittingCandidates,
}: CandidateReviewStepProps) {
  const reviewCount = Object.keys(reviews).length;
  const classification = importResult.classification_summary;
  const classificationSource =
    classification?.source === "AI"
      ? "AI 분류 적용"
      : classification?.source === "AI_SHADOW"
        ? "AI 분류 관찰"
        : "기존 규칙 기반 탐지";
  const classificationStatus =
    importResult.classification_status === "SUCCEEDED"
      ? "분류 완료"
      : importResult.classification_status === "REJECTED"
        ? "검증 거부"
        : importResult.classification_status === "FAILED"
          ? "호출 실패"
          : "호출하지 않음";

  return (
    <section className="setup-card card">
      <div className="setup-card-heading">
        <span className="setup-number">02</span>
        <div>
          <h3>찾은 패턴이 맞는지 확인해 주세요</h3>
          <p>확인하지 않은 예정 수입은 확정된 돈으로 계산하지 않습니다.</p>
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

      {classification && (
        <SubmitNotice
          kind={
            classification.source === "AI" && classification.applied
              ? "success"
              : "info"
          }
        >
          <span className="classification-summary-copy">
            <strong>
              자동 탐지 출처: {classificationSource} · {classificationStatus}
            </strong>
            {classification.source === "DETERMINISTIC_FALLBACK"
              ? "AI 분류를 적용하지 않고 기존 문자열 기준으로 후보를 찾았습니다."
              : `${classification.original_label_count}개 표기를 ${classification.grouped_entity_count}개 거래 대상으로 정리했고, ${classification.merged_label_count}개 표기를 통합했습니다.${
                  classification.applied
                    ? " 아래 결과를 확인해 주세요."
                    : " 현재 후보에는 적용하지 않았습니다."
                }${
                  classification.user_split_group_count
                    ? ` 사용자가 ${classification.user_split_group_count}개 그룹을 해제했습니다.`
                    : ""
                }`}
          </span>
        </SubmitNotice>
      )}

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
            return (
              <CandidateCard
                accounts={accounts}
                accountsError={accountsError}
                accountsLoading={accountsLoading}
                candidate={candidate}
                candidateDetails={candidateDetails}
                cards={cards}
                cardsError={cardsError}
                cardsLoading={cardsLoading}
                counterparties={counterparties}
                counterpartiesError={counterpartiesError}
                counterpartiesLoading={counterpartiesLoading}
                index={index}
                key={key}
                onReview={onReview}
                onSplit={onSplit}
                onUpdateCandidateDetail={onUpdateCandidateDetail}
                resolveCandidateDetails={resolveCandidateDetails}
                review={reviews[key]}
                splitBusy={splitBusy}
                splitError={
                  splitErrors[candidate.candidate_id || candidate.id || ""]
                }
                splitting={Boolean(
                  splittingCandidates[
                    candidate.candidate_id || candidate.id || ""
                  ],
                )}
              />
            );
          })}
        </div>
      ) : (
        <div className="no-candidates">
          <Icon name="info" />
          <p>
            서버가 자동 탐지 후보를 반환하지 않았습니다. 가져온 거래로 바로 분석을
            시작할 수 있습니다.
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
            disabled={busy || splitBusy}
            onClick={onReset}
            type="button"
          >
            서버 데이터 초기화 후 다시 선택
          </button>
          <button
            className="button button-primary"
            data-testid="setup-analyze"
            disabled={busy || splitBusy}
            onClick={onAnalyze}
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
  );
}

interface SetupCompleteStepProps {
  analysisResult: AnalysisResponse;
  importResult?: ImportResponse;
}

export function SetupCompleteStep({
  analysisResult,
  importResult,
}: SetupCompleteStepProps) {
  return (
    <section className="setup-complete card">
      <span className="complete-icon">
        <Icon name="check" size={31} />
      </span>
      <p className="eyebrow">Setup complete</p>
      <h3>첫 분석을 요청했어요.</h3>
      <p>
        서버가 금융 스냅숏을 만들고 13주 현금흐름을 분석합니다. 완료 전에는 임의의
        금액을 표시하지 않습니다.
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
        <span>금융 분석 상태</span>
        <strong>
          {analysisResult.analysis_status ||
            analysisResult.status ||
            "상태 정보 없음"}
        </strong>
        <span>AI 해석 상태</span>
        <strong>
          {analysisResult.interpretation_status || "상태 정보 없음"}
        </strong>
      </div>
      <Link className="button button-primary" href="/">
        대시보드에서 결과 확인
        <Icon name="arrow" size={17} />
      </Link>
    </section>
  );
}
