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
  SubmitNotice,
} from "@/components/ui";
import { ApiError, apiRequest, useRemote } from "@/lib/api";
import {
  formatDate,
  formatPercent,
  formatWon,
} from "@/lib/format";
import type { Account, Counterparty, Receivable } from "@/lib/types";

type ReceivablesResponse =
  | Receivable[]
  | {
      items?: Receivable[];
      data?: Receivable[];
      receivables?: Receivable[];
    };

type AccountsResponse =
  | Account[]
  | { items?: Account[]; data?: Account[]; accounts?: Account[] };
type CounterpartiesResponse =
  | Counterparty[]
  | {
      items?: Counterparty[];
      data?: Counterparty[];
      counterparties?: Counterparty[];
    };

function receivablesFrom(response?: ReceivablesResponse) {
  if (!response) return [];
  if (Array.isArray(response)) return response;
  return response.receivables || response.items || response.data || [];
}

function accountsFrom(response?: AccountsResponse) {
  if (!response) return [];
  if (Array.isArray(response)) return response;
  return response.accounts || response.items || response.data || [];
}

function counterpartiesFrom(response?: CounterpartiesResponse) {
  if (!response) return [];
  if (Array.isArray(response)) return response;
  return response.counterparties || response.items || response.data || [];
}

const statusCopy: Record<string, string> = {
  CONFIRMED: "일정 확인",
  ESTIMATED: "예상",
  OVERDUE: "입금 지연",
  RECEIVED: "입금 완료",
  CANCELLED: "취소",
};

export default function ReceivablesPage() {
  const { data, error, loading, reload } =
    useRemote<ReceivablesResponse>("/api/v1/receivables");
  const accountsRemote = useRemote<AccountsResponse>("/api/v1/accounts");
  const counterpartiesRemote =
    useRemote<CounterpartiesResponse>("/api/v1/counterparties");
  const [formOpen, setFormOpen] = useState(false);
  const [counterpartyId, setCounterpartyId] = useState("NEW");
  const [counterpartyName, setCounterpartyName] = useState("");
  const [amount, setAmount] = useState("");
  const [expectedDate, setExpectedDate] = useState("");
  const [accountId, setAccountId] = useState("");
  const [busy, setBusy] = useState(false);
  const [submitError, setSubmitError] = useState<string>();
  const [submitSuccess, setSubmitSuccess] = useState<string>();

  const receivables = useMemo(() => receivablesFrom(data), [data]);
  const accounts = useMemo(
    () => accountsFrom(accountsRemote.data),
    [accountsRemote.data],
  );
  const counterparties = useMemo(
    () => counterpartiesFrom(counterpartiesRemote.data),
    [counterpartiesRemote.data],
  );
  const active = receivables.filter(
    (item) => item.status !== "RECEIVED" && item.status !== "CANCELLED",
  );
  const upcomingTotal = active.reduce(
    (total, item) => total + (item.amount || 0),
    0,
  );
  const unconfirmed = active.filter((item) => !item.user_confirmed).length;
  const overdue = active.filter((item) => item.status === "OVERDUE").length;
  const isInitial404 = error instanceof ApiError && error.status === 404 && !data;

  async function registerReceivable(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!amount || Number(amount) <= 0 || !expectedDate) {
      setSubmitError("예정 금액과 입금 예정일을 올바르게 입력해 주세요.");
      return;
    }
    if (!accountId) {
      setSubmitError("입금 받을 계좌를 선택해 주세요.");
      return;
    }
    if (counterpartyId === "NEW" && !counterpartyName.trim()) {
      setSubmitError("새 거래처 이름을 입력해 주세요.");
      return;
    }

    setBusy(true);
    setSubmitError(undefined);
    setSubmitSuccess(undefined);
    try {
      let resolvedCounterpartyId = counterpartyId;
      if (counterpartyId === "NEW") {
        resolvedCounterpartyId = `counterparty-${crypto.randomUUID()}`;
        await apiRequest("/api/v1/counterparties", {
          method: "POST",
          body: JSON.stringify({
            counterparty_id: resolvedCounterpartyId,
            name: counterpartyName.trim(),
            counterparty_type: "CLIENT",
            is_recurring: false,
            payment_history_count: 0,
          }),
        });
      }

      await apiRequest("/api/v1/receivables", {
        method: "POST",
        body: JSON.stringify({
          receivable_id: `receivable-${crypto.randomUUID()}`,
          counterparty_id: resolvedCounterpartyId,
          amount: Number(amount),
          expected_date: expectedDate,
          status: "ESTIMATED",
          destination_account_id: accountId,
          user_confirmed: true,
          source: "USER_INPUT",
        }),
      });
      setSubmitSuccess("예정 수입을 등록했습니다. 새 분석에서 반영됩니다.");
      setCounterpartyId("NEW");
      setCounterpartyName("");
      setAmount("");
      setExpectedDate("");
      setAccountId("");
      reload();
      counterpartiesRemote.reload();
    } catch (caught) {
      setSubmitError(
        caught instanceof Error ? caught.message : "예정 수입을 등록하지 못했습니다.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (loading && !data) return <LoadingState label="예정 수입을 확인하고 있어요" />;
  if (error && !isInitial404 && !data) {
    return <ErrorState error={error} onRetry={reload} />;
  }

  return (
    <>
      <PageIntro
        eyebrow="Receivables"
        title="들어올 돈도, 늦어질 가능성까지."
        description="예정 수입을 확정된 현금처럼 보지 않고 거래처의 지급 이력과 사용자의 확인 상태를 함께 살펴봅니다."
        action={
          <button
            className="button button-primary"
            onClick={() => setFormOpen((current) => !current)}
            type="button"
          >
            <Icon name={formOpen ? "close" : "plus"} size={18} />
            {formOpen ? "등록 닫기" : "예정 수입 등록"}
          </button>
        }
      />

      {formOpen && (
        <section className="receivable-form card">
          <div>
            <p className="eyebrow">새 예정 수입</p>
            <h3>입금 계획을 알려주세요</h3>
            <p>확인되지 않은 금액은 보수적인 지연 시나리오로 분석됩니다.</p>
          </div>
          <form onSubmit={registerReceivable}>
            <div className="form-grid">
              <label className="field">
                <span>예정 금액</span>
                <input
                  className="input"
                  inputMode="numeric"
                  min="1"
                  onChange={(event) => setAmount(event.target.value)}
                  placeholder="예: 900000"
                  required
                  type="number"
                  value={amount}
                />
              </label>
              <label className="field">
                <span>입금 예정일</span>
                <input
                  className="input"
                  onChange={(event) => setExpectedDate(event.target.value)}
                  required
                  type="date"
                  value={expectedDate}
                />
              </label>
              <label className="field">
                <span>거래처</span>
                <select
                  className="select"
                  onChange={(event) => setCounterpartyId(event.target.value)}
                  value={counterpartyId}
                >
                  <option value="NEW">새 거래처 등록</option>
                  {counterparties.map((counterparty, index) => (
                    <option
                      key={counterparty.counterparty_id || index}
                      value={counterparty.counterparty_id}
                    >
                      {counterparty.name || counterparty.counterparty_id || "이름 정보 없음"}
                    </option>
                  ))}
                </select>
              </label>
              {counterpartyId === "NEW" && (
                <label className="field">
                  <span>새 거래처 이름</span>
                  <input
                    className="input"
                    onChange={(event) => setCounterpartyName(event.target.value)}
                    placeholder="예: 디자인컴퍼니"
                    value={counterpartyName}
                  />
                </label>
              )}
              <label className="field">
                <span>입금 받을 계좌</span>
                <select
                  className="select"
                  onChange={(event) => setAccountId(event.target.value)}
                  required
                  value={accountId}
                >
                  <option value="">계좌 선택</option>
                  {accounts.map((account, index) => (
                    <option
                      key={account.account_id || index}
                      value={account.account_id}
                    >
                      {account.name ||
                        account.account_name ||
                        account.account_id ||
                        "이름 정보 없음"}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            {accountsRemote.error && (
              <SubmitNotice kind="error">
                입금 계좌를 불러오지 못했습니다: {accountsRemote.error.message}
              </SubmitNotice>
            )}
            {!accountsRemote.loading && !accountsRemote.error && !accounts.length && (
              <SubmitNotice kind="info">
                등록 가능한 계좌가 없습니다. 먼저 거래 CSV를 연결해 주세요.
              </SubmitNotice>
            )}
            {submitError && <SubmitNotice kind="error">{submitError}</SubmitNotice>}
            {submitSuccess && (
              <SubmitNotice kind="success">{submitSuccess}</SubmitNotice>
            )}
            <div className="form-actions">
              <button
                className="button button-ghost"
                onClick={() => setFormOpen(false)}
                type="button"
              >
                취소
              </button>
              <button
                className="button button-primary"
                disabled={busy || !accounts.length}
                type="submit"
              >
                {busy ? "등록하는 중..." : "예정 수입 저장"}
              </button>
            </div>
          </form>
        </section>
      )}

      {receivables.length ? (
        <>
          <section className="receivable-summary card card-flat">
            <div>
              <span>진행 중 예정 수입</span>
              <strong>{active.length}건</strong>
            </div>
            <div>
              <span>예정 금액 합계</span>
              <strong>{formatWon(upcomingTotal)}</strong>
            </div>
            <div>
              <span>사용자 확인 필요</span>
              <strong>{unconfirmed}건</strong>
            </div>
            <div className={overdue ? "attention" : ""}>
              <span>입금 지연</span>
              <strong>{overdue}건</strong>
            </div>
          </section>

          <section>
            <SectionHeading
              eyebrow="Expected income"
              title="거래처별 입금 일정"
              description="상태와 지급 이력을 보고 실제 현금흐름에 어느 정도 신뢰로 반영됐는지 확인하세요."
            />
            <div className="receivable-list">
              {receivables.map((item, index) => {
                const evidence =
                  item.counterparty_evidence || item.evidence || {};
                const delay = item.average_delay_days ?? evidence.average_delay_days;
                const historyCount =
                  item.payment_history_count ?? evidence.payment_history_count;
                const confidence = item.data_confidence ?? evidence.data_confidence;
                const linkedCounterparty = counterparties.find(
                  (counterparty) =>
                    counterparty.counterparty_id === item.counterparty_id,
                );
                const name =
                  item.counterparty_name ||
                  item.counterparty?.name ||
                  linkedCounterparty?.name ||
                  item.counterparty_id ||
                  "거래처 정보 없음";
                return (
                  <article
                    className={`receivable-card card status-${item.status?.toLowerCase() || "unknown"}`}
                    key={item.receivable_id || item.event_id || index}
                  >
                    <div className="receivable-card-main">
                      <div className="counterparty-mark">
                        {name === "거래처 정보 없음" ? "?" : name.slice(0, 1)}
                      </div>
                      <div>
                        <div className="receivable-title-row">
                          <h3>{name}</h3>
                          <span className={`receivable-status rs-${item.status?.toLowerCase() || "unknown"}`}>
                            {item.status ? statusCopy[item.status] || item.status : "상태 정보 없음"}
                          </span>
                        </div>
                        <p>
                          {formatDate(item.expected_date, true)}
                          {item.destination_account_id
                            ? ` · ${item.destination_account_id}`
                            : ""}
                        </p>
                      </div>
                    </div>
                    <strong className="receivable-amount">
                      {typeof item.amount === "number"
                        ? formatWon(item.amount)
                        : "금액 정보 없음"}
                    </strong>
                    <div className="receivable-evidence">
                      <span>
                        <small>평균 입금 지연</small>
                        <strong>
                          {typeof delay === "number" ? `${delay}일` : "정보 없음"}
                        </strong>
                      </span>
                      <span>
                        <small>지급 이력</small>
                        <strong>
                          {typeof historyCount === "number"
                            ? `${historyCount}건`
                            : "정보 없음"}
                        </strong>
                      </span>
                      <span>
                        <small>데이터 축적도</small>
                        <strong>{formatPercent(confidence)}</strong>
                      </span>
                    </div>
                    {!item.user_confirmed &&
                      item.status !== "RECEIVED" &&
                      item.status !== "CANCELLED" && (
                        <div className="confirmation-note">
                          <Icon name="info" size={16} />
                          사용자가 확인하지 않은 예정 수입입니다.
                        </div>
                      )}
                  </article>
                );
              })}
            </div>
          </section>
        </>
      ) : (
        <EmptyState
          icon="receivables"
          title={isInitial404 ? "아직 첫 분석이 없어요" : "등록된 예정 수입이 없어요"}
          description={
            isInitial404
              ? "거래 CSV를 연결하거나 예정 수입을 직접 등록해 시작할 수 있어요."
              : "새 프로젝트 대금이나 현금 수입 일정을 등록하면 13주 분석에 반영합니다."
          }
          action={
            isInitial404 ? (
              <Link className="button button-secondary" href="/setup">
                CSV로 시작
              </Link>
            ) : undefined
          }
        />
      )}
    </>
  );
}
