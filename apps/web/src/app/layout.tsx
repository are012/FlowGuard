import type { Metadata } from "next";
import { Noto_Sans_KR } from "next/font/google";
import type { ReactNode } from "react";

import { AppShell } from "@/components/shell";

import "./globals.css";
import "../styles/pages/dashboard.css";
import "../styles/pages/setup.css";
import "../styles/pages/cashflow.css";
import "../styles/pages/risk.css";
import "../styles/pages/receivables.css";
import "../styles/pages/recommendations.css";
import "../styles/pages/installments.css";
import "../styles/responsive.css";

const notoSansKr = Noto_Sans_KR({
  display: "swap",
  preload: false,
  variable: "--font-noto-sans-kr",
  weight: ["400", "500", "600", "700", "800"],
});

export const metadata: Metadata = {
  title: {
    default: "FlowGuard | 오늘 쓸 수 있는 돈",
    template: "%s | FlowGuard",
  },
  description: "불규칙한 수입으로부터 오늘의 생활과 내일의 결제를 지키는 금융 안전망",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="ko">
      <body className={notoSansKr.variable}>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
