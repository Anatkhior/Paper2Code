import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/browser",
  timeout: 120_000,
  expect: { timeout: 30_000 },
  workers: 1,
  retries: 0,
  use: {
    baseURL: process.env.PAPER2CODE_WEB_URL || "http://localhost:3000",
    viewport: { width: 1440, height: 1000 },
    launchOptions: { executablePath: process.env.PAPER2CODE_CHROME || "/usr/bin/google-chrome", args: ["--no-sandbox"] },
    screenshot: "only-on-failure",
  },
});
