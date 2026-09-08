import { defineConfig } from "@playwright/test";
const port = Number(process.env.HORTATOR_TEST_PORT || "18000");
if (!Number.isInteger(port) || port < 1024 || port > 65535 || port === 8000) {
  throw new Error(
    "HORTATOR_TEST_PORT must be an unused test port (1024–65535, excluding 8000)",
  );
}
const baseURL = `http://127.0.0.1:${port}`;
export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  use: {
    baseURL,
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: `uv run --project .. python3.14 ../scripts/seed_test_trajectory.py /tmp/hortator-e2e-${process.pid} && uv run --project .. hortator --data-dir /tmp/hortator-e2e-${process.pid} serve --port ${port}`,
    url: `${baseURL}/api/health`,
    reuseExistingServer: false,
    timeout: 30000,
    env: {
      HORTATOR_ADMIN_PASSWORD: "test-only-password-never-use-in-production",
    },
  },
});
