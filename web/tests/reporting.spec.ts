import { expect, test } from "@playwright/test";

const password = "test-only-password-never-use-in-production";

test("overview polling stays small; full context loads on demand and is not repeatedly downloaded", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const fullReads: string[] = [];
  const summaryReads: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (/^\/api\/trajectory\/[^/]+$/.test(url.pathname)) {
      (url.searchParams.get("summary") === "true"
        ? summaryReads
        : fullReads
      ).push(url.pathname);
    }
  });
  await page.goto("/");
  await page.getByLabel("Dashboard password").fill(password);
  await page.getByRole("button", { name: "Enter council control" }).click();
  await page
    .getByRole("navigation", { name: "Workbench pages" })
    .getByRole("button", { name: "Trajectory", exact: true })
    .click();
  await page.locator(".turn-row").first().click();
  await expect(
    page.getByRole("heading", { name: "Turn timeline" }),
  ).toBeVisible();
  expect(summaryReads.length).toBeGreaterThan(0);
  expect(fullReads).toHaveLength(0);
  await page
    .locator(".inspector-tabs")
    .getByRole("button", { name: "context", exact: true })
    .click();
  await expect(
    page
      .getByText("Ordered messages sent to the provider", { exact: true })
      .first(),
  ).toBeVisible();
  expect(fullReads).toHaveLength(1);
  await page.waitForTimeout(6500); // Observe two real polling cycles on a completed fixture turn.
  expect(summaryReads.length).toBeGreaterThan(2);
  expect(fullReads).toHaveLength(1);
  await page
    .locator(".inspector-tabs")
    .getByRole("button", { name: "requests", exact: true })
    .click();
  await expect(
    page
      .getByText(
        "Provider reasoning & diagnostics (private; credentials redacted)",
      )
      .first(),
  ).toBeVisible();
  expect(fullReads).toHaveLength(1);
  expect(errors).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("reporting-inspector.png"),
  });
});

test("large event evidence is fetched when its summary is opened", async ({
  page,
}) => {
  const now = Date.now() / 1000;
  const brief = {
    id: "fixture-large-event",
    seq: 987654,
    at: now,
    kind: "context.assembled",
    level: "info",
    bot_id: "ada",
    turn_id: null,
    data: {},
    data_omitted: true,
  };
  let opened = 0;
  await page.route("**/api/events?*", (route) =>
    route.fulfill({ json: [brief] }),
  );
  await page.route("**/api/events/987654", (route) => {
    opened++;
    return route.fulfill({
      json: {
        ...brief,
        data_omitted: false,
        data: { preserved: "full fixture evidence" },
      },
    });
  });
  await page.goto("/");
  await page.getByLabel("Dashboard password").fill(password);
  await page.getByRole("button", { name: "Enter council control" }).click();
  await page
    .getByRole("navigation", { name: "Workbench pages" })
    .getByRole("button", { name: "Trajectory", exact: true })
    .click();
  await page.getByRole("button", { name: "Event ledger", exact: true }).click();
  await expect(page.locator(".ledger-event")).toContainText(
    "Open event for full details",
  );
  expect(opened).toBe(0);
  await page.locator(".ledger-event").click();
  await expect(page.getByRole("dialog")).toContainText("full fixture evidence");
  expect(opened).toBe(1);
});
