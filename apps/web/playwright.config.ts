import { defineConfig, devices } from "@playwright/test";
import { existsSync } from "node:fs";
import path from "node:path";

const API_URL = "http://127.0.0.1:8100";
const WEB_URL = "http://127.0.0.1:3100";
const webDirectory = __dirname;
const apiDirectory = path.resolve(webDirectory, "../api");
const databaseUrl =
  `sqlite:///file:flowguard-e2e-${process.pid}` +
  "?mode=memory&cache=shared&uri=true";
const pythonCandidates =
  process.platform === "win32"
    ? [
        path.resolve(apiDirectory, "../../.venv/Scripts/python.exe"),
        path.resolve(apiDirectory, "../../../../.venv/Scripts/python.exe"),
      ]
    : [
        path.resolve(apiDirectory, "../../.venv/bin/python"),
        path.resolve(apiDirectory, "../../../../.venv/bin/python"),
      ];
const pythonExecutable =
  process.env.FLOWGUARD_PYTHON ||
  pythonCandidates.find((candidate) => existsSync(candidate)) ||
  "python";
const applicationEnvironment = Object.fromEntries(
  Object.entries(process.env).filter(
    ([name, value]) =>
      typeof value === "string" &&
      name !== "FLOWGUARD_AGENT_MODE" &&
      !name.startsWith("OPENAI_"),
  ),
) as Record<string, string>;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["line"], ["html", { open: "never" }]] : "line",
  use: {
    baseURL: WEB_URL,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      command:
        `"${pythonExecutable}" -m uvicorn flowguard.main:app ` +
        "--host 127.0.0.1 --port 8100",
      cwd: apiDirectory,
      env: {
        ...applicationEnvironment,
        DATABASE_URL: databaseUrl,
        FLOWGUARD_AUTO_CREATE_SCHEMA: "true",
        FLOWGUARD_AI_SERVER_URL: "http://127.0.0.1:1",
        // 브라우저 시나리오는 결정론 경로와 동기 응답을 검증한다.
        // 프로덕션 기본값(비동기 + AI 조사)은 별도 실측으로 확인한다.
        FLOWGUARD_AI_INVESTIGATION: "off",
        FLOWGUARD_ANALYSIS_ASYNC: "off",
        FLOWGUARD_CORS_ORIGINS: WEB_URL,
        FLOWGUARD_DEMO_USER_ID: "e2e-user",
      },
      reuseExistingServer: false,
      timeout: 120_000,
      url: `${API_URL}/health`,
    },
    {
      command: "npm run dev -- --hostname 127.0.0.1 --port 3100",
      cwd: webDirectory,
      env: {
        ...applicationEnvironment,
        NEXT_PUBLIC_API_URL: API_URL,
      },
      reuseExistingServer: false,
      timeout: 120_000,
      url: `${WEB_URL}/setup`,
    },
  ],
});
