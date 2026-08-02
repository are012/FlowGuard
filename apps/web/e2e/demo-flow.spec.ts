import { expect, test, type Request } from "@playwright/test";
import path from "node:path";

import type { AnalysisStatus, InterpretationStatus } from "../src/lib/types";

const API_URL = "http://127.0.0.1:8100";
const DEMO_AS_OF = "2026-07-31T09:00:00+09:00";
const SAMPLE_CSV = path.resolve(
  __dirname,
  "../public/samples/flowguard-synthetic-transactions.csv",
);

type Account = {
  account_id: string;
  balance: number;
};

function accountBalances(accounts: Account[]) {
  return Object.fromEntries(
    accounts
      .map(({ account_id, balance }) => [account_id, balance] as const)
      .sort(([left], [right]) => left.localeCompare(right)),
  );
}

function analysisAsOf(request: Request) {
  const body = request.postDataJSON() as
    | {
        as_of?: string;
        analysis_as_of?: string;
        analysis?: { as_of?: string };
      }
    | undefined;
  return body?.as_of ?? body?.analysis_as_of ?? body?.analysis?.as_of;
}

test("sample data completes the safe virtual recommendation demo", async ({
  page,
  request,
}) => {
  const analysisRequests: Request[] = [];
  page.on("request", (candidate) => {
    const pathname = new URL(candidate.url()).pathname;
    if (
      candidate.method() === "POST" &&
      (pathname === "/api/v1/analyses" || pathname === "/api/v1/setup/commit")
    ) {
      analysisRequests.push(candidate);
    }
  });

  await page.goto("/setup");

  await page.getByTestId("setup-file-input").setInputFiles(SAMPLE_CSV);
  const importResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === "/api/v1/imports/transactions",
  );
  await page.getByTestId("setup-upload").click();

  const importResponse = await importResponsePromise;
  expect(importResponse.status()).toBe(201);
  const imported = (await importResponse.json()) as {
    revision?: string;
    analysis_required?: boolean;
  };
  expect(imported.revision).toEqual(expect.any(String));
  expect(imported.analysis_required).toBe(true);

  const accountsBeforeResponse = await request.get(`${API_URL}/api/v1/accounts`);
  expect(accountsBeforeResponse.ok()).toBe(true);
  const balancesBefore = accountBalances(
    (await accountsBeforeResponse.json()) as Account[],
  );

  await page.getByTestId("setup-analyze").click();
  await expect(page.locator(".setup-complete")).toBeVisible();
  await expect(page.locator(".analysis-request-info")).toContainText("SUCCEEDED");
  await expect(page.locator(".analysis-request-info")).toContainText("FALLBACK");

  const setupCommitRequests = analysisRequests.filter(
    (candidate) => new URL(candidate.url()).pathname === "/api/v1/setup/commit",
  );
  const analysisRunRequests = analysisRequests.filter(
    (candidate) => new URL(candidate.url()).pathname === "/api/v1/analyses",
  );
  expect(setupCommitRequests).toHaveLength(1);
  expect(analysisRunRequests).toHaveLength(1);
  expect(analysisAsOf(analysisRunRequests[0])).toBe(DEMO_AS_OF);

  await page.locator('.setup-complete a[href="/"]').click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByText("결정론적 금융 분석", { exact: true })).toBeVisible();
  await expect(
    page.getByText("AI 해석 · 규칙 기반 설명", { exact: true }),
  ).toBeVisible();

  const dashboardResponse = await request.get(`${API_URL}/api/v1/dashboard`);
  expect(dashboardResponse.ok()).toBe(true);
  const dashboard = (await dashboardResponse.json()) as {
    analysis_status?: AnalysisStatus;
    interpretation_status?: InterpretationStatus;
    execution_stage?: string;
    report_revision?: string;
    latest_data_revision?: string;
    is_stale?: boolean;
    refresh_status?: AnalysisStatus;
    interpretation?: { source?: string } | null;
  };
  expect(dashboard.analysis_status).toBe("SUCCEEDED");
  expect(dashboard.interpretation_status).toBe("FALLBACK");
  expect(dashboard.execution_stage).toBe("COMPLETED");
  expect(dashboard.report_revision).toBe(dashboard.latest_data_revision);
  expect(dashboard.is_stale).toBe(false);
  expect(dashboard.refresh_status).toBe("SUCCEEDED");
  expect(dashboard.interpretation?.source).toBe("DETERMINISTIC_FALLBACK");

  await page.goto("/cashflow");
  await expect(page.getByText("결제계좌 안전여유", { exact: false }).first()).toBeVisible();
  await expect(page.getByText("안전기준 미달", { exact: true })).toBeVisible();
  await expect(page.getByText("-250,000원", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/월세 500,000원이/).first()).toBeVisible();
  await expect(page.getByText("예정된 주요 위험 없음").first()).toBeVisible();

  await page.goto("/recommendations");
  await expect(page).toHaveURL(/\/recommendations$/);

  const comparison = page.getByTestId("recommendation-comparison");
  await expect(comparison).toBeVisible();
  const comparedPlans = comparison.locator(
    "[data-candidate-id], tbody tr, article",
  );
  expect(await comparedPlans.count()).toBeGreaterThanOrEqual(2);
  await expect(page.getByTestId("recommendation-evidence")).toBeVisible();

  const approvalResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      /\/api\/v1\/recommendations\/[^/]+\/approve$/.test(
        new URL(response.url()).pathname,
      ),
  );
  await page.getByTestId("recommendation-approve").click();

  const approvalResponse = await approvalResponsePromise;
  expect(approvalResponse.ok()).toBe(true);
  const approval = (await approvalResponse.json()) as {
    virtual_application?: { external_actions_executed?: boolean };
    virtual_analysis?: {
      status?: string;
      analysis_status?: AnalysisStatus;
      interpretation_status?: InterpretationStatus;
    };
    virtual_report?: { is_virtual?: boolean };
  };
  expect(approval.virtual_application?.external_actions_executed).toBe(false);
  expect(approval.virtual_analysis?.analysis_status).toBe("SUCCEEDED");
  expect(approval.virtual_report?.is_virtual).toBe(true);

  const accountsAfterResponse = await request.get(`${API_URL}/api/v1/accounts`);
  expect(accountsAfterResponse.ok()).toBe(true);
  expect(
    accountBalances((await accountsAfterResponse.json()) as Account[]),
  ).toEqual(balancesBefore);

  await page.goto("/setup");
  page.once("dialog", (dialog) => dialog.accept());
  const resetResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === "/api/v1/demo/reset",
  );
  await page.getByTestId("demo-reset").click();

  const resetResponse = await resetResponsePromise;
  expect(resetResponse.ok()).toBe(true);
  await expect(page.getByTestId("setup-upload")).toBeVisible();

  const emptyAccountsResponse = await request.get(`${API_URL}/api/v1/accounts`);
  expect(emptyAccountsResponse.ok()).toBe(true);
  expect(await emptyAccountsResponse.json()).toEqual([]);

  const emptyRecommendationsResponse = await request.get(
    `${API_URL}/api/v1/recommendations`,
  );
  expect(emptyRecommendationsResponse.ok()).toBe(true);
  expect(await emptyRecommendationsResponse.json()).toEqual([]);
});
