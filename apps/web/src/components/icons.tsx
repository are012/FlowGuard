import type { SVGProps } from "react";

export type IconName =
  | "home"
  | "setup"
  | "cashflow"
  | "risk"
  | "receivables"
  | "installments"
  | "recommendations"
  | "arrow"
  | "refresh"
  | "upload"
  | "download"
  | "check"
  | "calendar"
  | "shield"
  | "wallet"
  | "plus"
  | "close"
  | "info";

const paths: Record<IconName, React.ReactNode> = {
  home: (
    <>
      <path d="m3 10 9-7 9 7" />
      <path d="M5 9.5V21h14V9.5M9 21v-7h6v7" />
    </>
  ),
  setup: (
    <>
      <path d="M12 3v3M12 18v3M3 12h3M18 12h3" />
      <circle cx="12" cy="12" r="4" />
      <path d="m5.6 5.6 2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1" />
    </>
  ),
  cashflow: (
    <>
      <path d="M4 19V9M10 19V5M16 19v-7M22 19V3" />
      <path d="M2 19h22" />
    </>
  ),
  risk: (
    <>
      <path d="M12 3 2.8 20h18.4L12 3Z" />
      <path d="M12 9v5M12 17.5v.1" />
    </>
  ),
  receivables: (
    <>
      <rect x="3" y="5" width="18" height="15" rx="3" />
      <path d="M3 10h18M8 3v4M16 3v4M8 15h3" />
    </>
  ),
  installments: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="3" />
      <path d="M3 9h18M7 15h3M15 13v4M13 15h4" />
    </>
  ),
  recommendations: (
    <>
      <path d="M9 18h6M10 22h4" />
      <path d="M8.5 15.5A7 7 0 1 1 15.5 15.5c-.9.7-1.5 1.4-1.5 2.5h-4c0-1.1-.6-1.8-1.5-2.5Z" />
    </>
  ),
  arrow: <path d="M5 12h14M14 7l5 5-5 5" />,
  refresh: (
    <>
      <path d="M20 6v5h-5" />
      <path d="M19 11a8 8 0 1 0 .2 4" />
    </>
  ),
  upload: (
    <>
      <path d="M12 16V4M7 9l5-5 5 5" />
      <path d="M4 15v5h16v-5" />
    </>
  ),
  download: (
    <>
      <path d="M12 4v12M7 11l5 5 5-5" />
      <path d="M4 20h16" />
    </>
  ),
  check: <path d="m4 12 5 5L20 6" />,
  calendar: (
    <>
      <rect x="3" y="5" width="18" height="16" rx="3" />
      <path d="M3 10h18M8 3v4M16 3v4" />
    </>
  ),
  shield: (
    <>
      <path d="M12 3 4 6v6c0 5.2 3.3 8.1 8 9 4.7-.9 8-3.8 8-9V6l-8-3Z" />
      <path d="m8.5 12 2.3 2.3 4.8-5" />
    </>
  ),
  wallet: (
    <>
      <path d="M4 6.5A2.5 2.5 0 0 1 6.5 4H19v16H6a2 2 0 0 1-2-2V6.5Z" />
      <path d="M4 8h15M15 12h6v5h-6a2.5 2.5 0 0 1 0-5Z" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  close: <path d="m6 6 12 12M18 6 6 18" />,
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v6M12 7.5v.1" />
    </>
  ),
};

interface IconProps extends SVGProps<SVGSVGElement> {
  name: IconName;
  size?: number;
}

export function Icon({ name, size = 20, ...props }: IconProps) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height={size}
      viewBox="0 0 24 24"
      width={size}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.8"
      {...props}
    >
      {paths[name]}
    </svg>
  );
}
