"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { Icon, type IconName } from "@/components/icons";

const navItems: Array<{ href: string; label: string; icon: IconName }> = [
  { href: "/", label: "홈", icon: "home" },
  { href: "/cashflow", label: "현금흐름", icon: "cashflow" },
  { href: "/risk", label: "결제 위험", icon: "risk" },
  { href: "/receivables", label: "예정 수입", icon: "receivables" },
  { href: "/installments", label: "할부부담", icon: "installments" },
  { href: "/recommendations", label: "추천안", icon: "recommendations" },
];

const pageTitles: Record<string, string> = {
  "/": "오늘의 FlowGuard",
  "/setup": "처음 설정",
  "/cashflow": "13주 현금흐름",
  "/risk": "가장 가까운 위험",
  "/receivables": "예정 수입",
  "/installments": "할부부담",
  "/recommendations": "추천안",
};

function isActive(pathname: string, href: string) {
  return href === "/" ? pathname === "/" : pathname.startsWith(href);
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Link className="brand" href="/" aria-label="FlowGuard 홈">
          <span className="brand-mark">
            <span />
            <span />
            <span />
          </span>
          <span className="brand-copy">
            <strong>FlowGuard</strong>
            <small>흐름을 지키는 금융 안전망</small>
          </span>
        </Link>

        <Link className="setup-link" href="/setup">
          <span className="nav-icon">
            <Icon name="setup" />
          </span>
          <span>
            <strong>내 데이터 연결</strong>
            <small>CSV로 2분 안에 시작</small>
          </span>
          <Icon name="arrow" size={17} />
        </Link>

        <nav className="desktop-nav" aria-label="주요 메뉴">
          <p className="nav-label">내 금융 흐름</p>
          {navItems.map((item) => (
            <Link
              className={`nav-item ${isActive(pathname, item.href) ? "active" : ""}`}
              href={item.href}
              key={item.href}
            >
              <Icon name={item.icon} />
              <span>{item.label}</span>
            </Link>
          ))}
        </nav>

        <div className="sidebar-note">
          <Icon name="shield" />
          <div>
            <strong>실제 금융거래는 실행하지 않아요</strong>
            <p>모든 추천은 먼저 가상으로 검증합니다.</p>
          </div>
        </div>

        <div className="sidebar-profile">
          <span className="profile-avatar">
            <Icon name="shield" size={17} />
          </span>
          <span>
            <strong>MVP 가상 분석</strong>
            <small>합성 데이터 전용 환경</small>
          </span>
          <span className="profile-dot" />
        </div>
      </aside>

      <div className="main-area">
        <header className="topbar">
          <div className="mobile-brand">
            <span className="brand-mark small">
              <span />
              <span />
              <span />
            </span>
            <strong>FlowGuard</strong>
          </div>
          <h1>{pageTitles[pathname] || "FlowGuard"}</h1>
          <div className="topbar-badge">
            <span />
            가상 분석 모드
          </div>
        </header>
        <main className="page-content">{children}</main>
      </div>

      <nav className="mobile-nav" aria-label="모바일 주요 메뉴">
        {navItems.map((item) => (
          <Link
            className={isActive(pathname, item.href) ? "active" : ""}
            href={item.href}
            key={item.href}
          >
            <Icon name={item.icon} size={19} />
            <span>{item.label}</span>
          </Link>
        ))}
      </nav>
    </div>
  );
}
