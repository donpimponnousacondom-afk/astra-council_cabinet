import { expect, test } from "@playwright/test";

test("zero disables the timer and positive values restore it without changing other bot settings", async ({
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
  const id = "wb-timer-fixture";
  const created = await page.request.post("/api/control", {
    headers: { "X-CSRF-Token": session.csrf },
    data: {
      action: "create",
      kind: "bots",
      id,
      data: {
        id,
        name: id,
        model_profile_id: "balanced",
        enabled: false,
        interval_seconds: 60,
        cooldown_seconds: 12,
        evaluate_when_idle: true,
        enabled_plugins: ["slash_commands"],
      },
    },
  });
  expect(created.ok()).toBe(true);
  const original = await created.json();
  await page
    .getByRole("button", { name: "Refresh dashboard", exact: true })
    .click();
  await page
    .getByRole("navigation", { name: "Workbench pages" })
    .getByRole("button", { name: "Bots", exact: true })
    .click();
  await page.getByRole("button", { name: `Edit ${id}`, exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("button", { name: "Model & rhythm", exact: true })
    .click();
  const interval = dialog.getByRole("spinbutton", {
    name: "Activation interval (seconds)",
    exact: true,
  });
  await expect(interval).toHaveAttribute("min", "0");
  await interval.fill("-1");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  expect(
    await interval.evaluate((input: HTMLInputElement) => input.checkValidity()),
  ).toBe(false);
  await interval.fill("0");
  await expect(dialog).toContainText(
    "Timer off: the bot still observes its assigned rooms.",
  );
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("row").filter({
      has: page.getByRole("button", { name: `Edit ${id}`, exact: true }),
    }),
  ).toContainText("Off / 12s");
  const saved = await (await page.request.get(`/api/config/bots/${id}`)).json();
  expect(saved.interval_seconds).toBe(0);
  for (const key of [
    "enabled",
    "cooldown_seconds",
    "evaluate_when_idle",
    "enabled_plugins",
    "room_ids",
  ])
    expect(saved[key]).toEqual(original[key]);
  await page.getByRole("button", { name: `Edit ${id}`, exact: true }).click();
  await dialog
    .getByRole("button", { name: "Model & rhythm", exact: true })
    .click();
  await expect(interval).toHaveValue("0");
  await interval.fill("60");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const restored = await (
    await page.request.get(`/api/config/bots/${id}`)
  ).json();
  expect(restored.interval_seconds).toBe(60);
  expect(restored.cooldown_seconds).toBe(12);
  expect(restored.evaluate_when_idle).toBe(true);
});
