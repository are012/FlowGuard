import Link from "next/link";
import type { ReactNode } from "react";

import { Icon, type IconName } from "@/components/icons";
import { normalizeStatus, statusLabel } from "@/lib/format";
import type { RiskStatus } from "@/lib/types";

export function PageIntro({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <header className="page-intro">
      <div>
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <h2>{title}</h2>
        {description && <p>{description}</p>}
      </div>
      {action && <div className="page-intro-action">{action}</div>}
    </header>
  );
}

export function StatusPill({
  status,
  label,
}: {
  status?: string;
  label?: string;
}) {
  const normalized = normalizeStatus(status);
  return (
    <span
      className={`status-pill ${
        normalized ? `status-${normalized.toLowerCase()}` : "status-unknown"
      }`}
    >
      <span />
      {label || (normalized ? statusLabel[normalized] : "상태 정보 없음")}
    </span>
  );
}

export function LoadingState({ label = "금융 흐름을 불러오고 있어요" }: { label?: string }) {
  return (
    <div className="state-panel loading-panel" role="status">
      <span className="loading-orbit">
        <span />
      </span>
      <div>
        <strong>{label}</strong>
        <p>최신 분석 결과를 안전하게 확인하는 중입니다.</p>
      </div>
      <div className="skeleton-lines" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
}: {
  error: Error;
  onRetry?: () => void;
}) {
  return (
    <div className="state-panel error-panel" role="alert">
      <span className="state-icon">
        <Icon name="risk" size={24} />
      </span>
      <div>
        <strong>분석 정보를 불러오지 못했어요</strong>
        <p>{error.message}</p>
        <small>API 서버 연결과 환경 설정을 확인해 주세요.</small>
      </div>
      {onRetry && (
        <button className="button button-secondary" onClick={onRetry} type="button">
          <Icon name="refresh" size={17} />
          다시 불러오기
        </button>
      )}
    </div>
  );
}

export function EmptyState({
  icon = "info",
  title,
  description,
  hint,
  action,
}: {
  icon?: IconName;
  title: string;
  description: string;
  /** 비어 있는 이유만으로 부족할 때, 사용자가 다음에 할 일을 덧붙인다. */
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="state-panel empty-panel">
      <span className="state-icon">
        <Icon name={icon} size={25} />
      </span>
      <div>
        <strong>{title}</strong>
        <p>{description}</p>
        {hint && (
          <p className="empty-hint">
            <Icon name="arrow" size={15} />
            {hint}
          </p>
        )}
      </div>
      {action}
    </div>
  );
}

export function SectionHeading({
  eyebrow,
  title,
  description,
  aside,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  aside?: ReactNode;
}) {
  return (
    <div className="section-heading">
      <div>
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <h3>{title}</h3>
        {description && <p>{description}</p>}
      </div>
      {aside}
    </div>
  );
}

export function InlineLink({
  href,
  children,
}: {
  href: string;
  children: ReactNode;
}) {
  return (
    <Link className="inline-link" href={href}>
      {children}
      <Icon name="arrow" size={16} />
    </Link>
  );
}

export function DataFact({
  icon,
  label,
  value,
  detail,
}: {
  icon: IconName;
  label: string;
  value: string;
  detail?: string;
}) {
  return (
    <div className="data-fact">
      <span className="fact-icon">
        <Icon name={icon} />
      </span>
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
        {detail && <small>{detail}</small>}
      </div>
    </div>
  );
}

export function StatusComparison({
  before,
  after,
}: {
  before?: RiskStatus | string;
  after?: RiskStatus | string;
}) {
  return (
    <div className="status-comparison">
      <div>
        <span>적용 전</span>
        <StatusPill status={before} />
      </div>
      <span className="comparison-arrow">
        <Icon name="arrow" />
      </span>
      <div>
        <span>가상 적용 후</span>
        <StatusPill status={after} />
      </div>
    </div>
  );
}

export function SubmitNotice({
  kind,
  children,
}: {
  kind: "success" | "error" | "info";
  children: ReactNode;
}) {
  return (
    <div className={`submit-notice notice-${kind}`} role={kind === "error" ? "alert" : "status"}>
      <Icon name={kind === "success" ? "check" : kind === "error" ? "risk" : "info"} size={18} />
      <span>{children}</span>
    </div>
  );
}
