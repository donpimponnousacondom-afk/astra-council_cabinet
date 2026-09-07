import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  use: {
    baseURL: "http://127.0.0.1:18000",
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: `uv run --project .. python ../scripts/seed_test_trajectory.py /tmp/hortator-e2e-${process.pid} && uv run --project .. hortator --data-dir /tmp/hortator-e2e-${process.pid} serve --port 18000`,
    url: "http://127.0.0.1:18000/api/health",
    reuseExistingServer: false,
    timeout: 30000,
    env: {
      HORTATOR_ADMIN_PASSWORD: "test-only-password-never-use-in-production",
    },
  },
});
