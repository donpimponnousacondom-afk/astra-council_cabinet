import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  // Loopback is considered secure by browsers. Reproduce the missing API seen
  // on ordinary HTTP origins without changing authentication or server policy.
  await page.addInitScript(() => {
    Object.defineProperty(crypto, "randomUUID", { value: undefined });
  });
  await page.goto("/");
  await page
    .getByLabel("Dashboard password")
    .fill("test-only-password-never-use-in-production");
  await page.getByRole("button", { name: "Enter council control" }).click();
  await expect(
    page.getByRole("heading", { name: "The council", exact: true, level: 1 }),
  ).toBeVisible();
});

test("all new-record editors open without randomUUID", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  expect(await page.evaluate(() => typeof crypto.randomUUID)).toBe("undefined");
  for (const [section, button, prefix] of [
    ["Bots", "Add bot", "bot"],
    ["Providers", "Add provider", "provider"],
    ["Model profiles", "Add profile", "model"],
    ["Prompt library", "Add prompt", "prompt"],
    ["Rooms", "Add room", "room"],
  ]) {
    await page
      .getByRole("navigation", { name: "Workbench pages" })
      .getByRole("button", { name: section, exact: true })
      .click();
    await page.getByRole("button", { name: button, exact: true }).click();
    const dialog = page.getByRole("dialog");
    await expect(
      dialog.getByLabel("Stable identifier", { exact: true }),
    ).toHaveValue(new RegExp(`^${prefix}_[0-9a-f]{8}$`));
    await dialog
      .getByRole("button", { name: "Close dialog", exact: true })
      .click();
    await expect(dialog).not.toBeVisible();
  }
  expect(errors).toEqual([]);
});

test("Engram prompts save under generated IDs without randomUUID", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page
    .getByRole("navigation", { name: "Workbench pages" })
    .getByRole("button", { name: "Prompt library", exact: true })
    .click();
  const ids: string[] = [];
  for (const name of ["HTTP Engram fixture one", "HTTP Engram fixture two"]) {
    await page.getByRole("button", { name: "Add prompt", exact: true }).click();
    const dialog = page.getByRole("dialog");
    const id = await dialog
      .getByLabel("Stable identifier", { exact: true })
      .inputValue();
    expect(id).toMatch(/^prompt_[0-9a-f]{8}$/);
    expect(ids).not.toContain(id);
    ids.push(id);
    await dialog.getByLabel("Display name", { exact: true }).fill(name);
    await dialog
      .getByLabel("Prompt placement", { exact: true })
      .selectOption("engram_instructions");
    await dialog
      .getByLabel("System prompt", { exact: true })
      .fill("Preserve attributed facts and corrections.");
    await dialog
      .getByRole("button", { name: "Create draft", exact: true })
      .click();
    await expect(dialog).not.toBeVisible();
    const response = await page.request.get(`/api/config/prompts/${id}`);
    expect(response.ok()).toBe(true);
    expect(await response.json()).toMatchObject({
      id,
      name,
      runtime_layer: "engram_instructions",
      content: "Preserve attributed facts and corrections.",
    });
  }
  expect(errors).toEqual([]);
});
