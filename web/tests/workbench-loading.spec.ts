import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

async function login(page: Page) {
  await page.goto("/");
  await page
    .getByLabel("Dashboard password")
    .fill("test-only-password-never-use-in-production");
  await page.getByRole("button", { name: "Enter council control" }).click();
}

test("failed schema loading cannot open an empty editor and retries without losing the session", async ({
  page,
}) => {
  let fail = true;
  await page.route("**/api/config-schemas", async (route) => {
    if (fail)
      await route.fulfill({
        status: 503,
        json: { error: "Synthetic schema outage" },
      });
    else await route.continue();
  });
  await login(page);
  await expect(
    page.getByText("Synthetic schema outage", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Add bot", exact: true }),
  ).toHaveCount(0);
  expect((await page.request.get("/api/auth/session")).status()).toBe(200);
  fail = false;
  await page.getByRole("button", { name: "Retry connection" }).click();
  await page
    .getByRole("navigation", { name: "Workbench pages" })
    .getByRole("button", { name: "Bots", exact: true })
    .click();
  await page.getByRole("button", { name: "Edit Ada", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Identity", exact: true })
    .click();
  await expect(
    page.getByRole("dialog").getByLabel("Stable identifier", { exact: true }),
  ).toHaveValue("ada");
  await expect(
    page.getByRole("dialog").getByLabel("Display name", { exact: true }),
  ).toHaveValue("Ada");
});

test("a delayed thread receipt preserves names typed in either room while waiting", async ({
  page,
}) => {
  // Synthetic status and delivery only: never create a real Discord thread.
  await page.route("**/api/status", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.rooms = ["First room", "Second room"].map((name, index) => ({
      ...data.rooms[0],
      id: `thread-fixture-${index}`,
      name,
    }));
    await route.fulfill({ json: data });
  });
  let release!: () => void;
  let received!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  const started = new Promise<void>((resolve) => {
    received = resolve;
  });
  await page.route("**/api/control", async (route) => {
    const body = route.request().postDataJSON();
    if (body.action !== "thread") return route.continue();
    expect(body.data.name).toBe("Submitted thread");
    received();
    await held;
    await route.fulfill({ json: { thread_id: "synthetic-thread-receipt" } });
  });
  await login(page);
  await page
    .getByRole("navigation", { name: "Workbench pages" })
    .getByRole("button", { name: "Rooms", exact: true })
    .click();
  const first = page.getByRole("textbox", { name: "New thread in First room" });
  const second = page.getByRole("textbox", {
    name: "New thread in Second room",
  });
  await first.fill("Submitted thread");
  try {
    await page
      .getByRole("button", { name: "Create thread in First room" })
      .click();
    await started;
    await second.fill("Keep this other room draft");
    await first.fill("Keep this newer first room draft");
  } finally {
    release();
  }
  await expect(
    page.getByRole("status").filter({ hasText: "Thread created:" }),
  ).toContainText("synthetic-thread-receipt");
  await expect(first).toHaveValue("Keep this newer first room draft");
  await expect(second).toHaveValue("Keep this other room draft");
});
