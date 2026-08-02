import { describe, expect, it } from "vitest";

import { deriveWeeklyPositions, formatDate, formatWon } from "@/lib/format";
import type { DailyPosition } from "@/lib/types";

describe("formatWon", () => {
  it("formats positive, negative, and rounded won amounts", () => {
    expect(formatWon(1_234_567)).toBe("1,234,567원");
    expect(formatWon(-42_000)).toBe("-42,000원");
    expect(formatWon(12.6)).toBe("13원");
  });
});

describe("formatDate", () => {
  it("formats date-only values in the Seoul time zone", () => {
    expect(formatDate("2026-08-02")).toBe("8월 2일 (일)");
    expect(formatDate("2026-08-02", true)).toBe("2026년 8월 2일 일");
  });

  it("converts timestamp values to the Seoul calendar date", () => {
    expect(formatDate("2026-08-01T16:00:00Z")).toBe("8월 2일 (일)");
  });

  it("returns the fallback for missing or invalid values", () => {
    expect(formatDate()).toBe("날짜 정보 없음");
    expect(formatDate("not-a-date")).toBe("날짜 정보 없음");
  });
});

describe("deriveWeeklyPositions", () => {
  it("groups consecutive days into weeks and keeps the worst status and balance", () => {
    const daily: DailyPosition[] = [
      { date: "2026-08-01", available_balance: 80_000, status: "STABLE" },
      { date: "2026-08-02", available_balance: 45_000, status: "VERIFY" },
      { date: "2026-08-03", available_balance: 60_000, status: "PREPARE" },
      { date: "2026-08-04", available_balance: 30_000, status: "ACT_NOW" },
      { date: "2026-08-05", available_balance: 50_000, status: "STABLE" },
      { date: "2026-08-06", available_balance: 70_000 },
      { date: "2026-08-07", available_balance: 90_000, status: "VERIFY" },
      { date: "2026-08-08", available_balance: 120_000, status: "STABLE" },
    ];

    expect(deriveWeeklyPositions(daily)).toEqual([
      {
        week: 1,
        start_date: "2026-08-01",
        end_date: "2026-08-07",
        min_available_balance: 30_000,
        status: "ACT_NOW",
        causes: [],
      },
      {
        week: 2,
        start_date: "2026-08-08",
        end_date: "2026-08-08",
        min_available_balance: 120_000,
        status: "STABLE",
        causes: [],
      },
    ]);
  });

  it("preserves undefined aggregates when positions have no usable values", () => {
    expect(deriveWeeklyPositions([{ date: "2026-08-01" }])).toEqual([
      {
        week: 1,
        start_date: "2026-08-01",
        end_date: "2026-08-01",
        min_available_balance: undefined,
        status: undefined,
        causes: [],
      },
    ]);
    expect(deriveWeeklyPositions([])).toEqual([]);
  });
});
