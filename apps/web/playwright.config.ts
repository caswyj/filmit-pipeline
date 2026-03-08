import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

const repoOutputRoot = path.resolve(process.cwd(), "..", "..", "output", "playwright");
const defaultBaseUrl = process.env.N2V_WEB_URL ?? "http://127.0.0.1:3100";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 2 : undefined,
  timeout: 60_000,
  reporter: [
    ["list"],
    ["html", { outputFolder: path.join(repoOutputRoot, "html-report"), open: "never" }],
  ],
  outputDir: path.join(repoOutputRoot, "test-results"),
  use: {
    baseURL: defaultBaseUrl,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  webServer: process.env.N2V_WEB_URL
    ? undefined
    : {
        command: "npm run dev -- --hostname 127.0.0.1 --port 3100",
        url: defaultBaseUrl,
        reuseExistingServer: true,
        timeout: 120_000,
      },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 1200 },
      },
    },
  ],
});
