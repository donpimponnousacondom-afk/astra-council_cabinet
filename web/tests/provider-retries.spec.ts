import { expect, test } from "@playwright/test";

test("provider retries default to three and ten seconds and save independently of circuit recovery", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByLabel("Dashboard password")
    .fill("test-only-password-never-use-in-production");
  await page.getByRole("button", { name: "Enter council control" }).click();
  await expect(
    page.getByRole("heading", { name: "The council", exact: true, level: 1 }),
  ).toBeVisible();
  const session = await (await page.request.get("/api/auth/session")).json();
  const id = "retry-fixture";
  const created = await page.request.post("/api/control", {
    headers: { "X-CSRF-Token": session.csrf },
    data: {
      action: "create",
      kind: "providers",
      id,
      data: {
        id,
        name: id,
        base_url: "https://provider.test/v1",
        enabled: false,
      },
    },
  });
  expect(created.ok()).toBe(true);
  const original = await created.json();
  expect(original.retry_count).toBe(3);
  expect(original.retry_delay_seconds).toBe(10);
  await page
    .getByRole("button", { name: "Refresh dashboard", exact: true })
    .click();
  await page
    .getByRole("navigation", { name: "Workbench pages" })
    .getByRole("button", { name: "Providers", exact: true })
    .click();
  await page.getByRole("button", { name: `Edit ${id}`, exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "Limits", exact: true }).click();
  const retries = dialog.getByRole("spinbutton", {
    name: "Retries per model request",
    exact: true,
  });
  const delay = dialog.getByRole("spinbutton", {
    name: "Retry delay (seconds)",
    exact: true,
  });
  await expect(retries).toHaveValue("3");
  await expect(delay).toHaveValue("10");
  await retries.fill("2");
  await delay.fill("5");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const saved = await (
    await page.request.get(`/api/config/providers/${id}`)
  ).json();
  expect(saved.retry_count).toBe(2);
  expect(saved.retry_delay_seconds).toBe(5);
  expect(saved.failure_threshold).toBe(original.failure_threshold);
  expect(saved.circuit_seconds).toBe(original.circuit_seconds);
});
