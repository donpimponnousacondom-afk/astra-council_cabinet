import { expect, test } from "@playwright/test";

test("DeepSeek search configures independently without enabling it or changing other search tools", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByLabel("Dashboard password")
    .fill("test-only-password-never-use-in-production");
  await page.getByRole("button", { name: "Enter council control" }).click();
  await expect(
    page.getByRole("heading", { name: "The council", exact: true }),
  ).toBeVisible();
  const session = await (await page.request.get("/api/auth/session")).json();
  const created = await page.request.post("/api/control", {
    headers: { "X-CSRF-Token": session.csrf },
    data: {
      action: "create",
      kind: "providers",
      id: "deepseek-search-browser",
      data: {
        name: "DeepSeek Search Browser",
        base_url: "https://api.deepseek.com/v1",
      },
    },
  });
  expect(created.ok()).toBeTruthy();
  const before = await (
    await page.request.get("/api/config/plugins/deepseek_search")
  ).json();
  expect(before.enabled).toBe(false);
  expect(before.keyless).toBe(true);
  expect(before.config.count).toBe(5);
  const webSearchBefore = await (
    await page.request.get("/api/config/plugins/web_search")
  ).json();
  const parallelBefore = await (
    await page.request.get("/api/config/plugins/dumb_search")
  ).json();
  const botBefore = await (
    await page.request.get("/api/config/bots/ada")
  ).json();
  expect(botBefore.enabled_plugins).not.toContain("deepseek_search");
  try {
    await page
      .getByRole("navigation", { name: "Workbench pages" })
      .getByRole("button", { name: "Plugins", exact: true })
      .click();
    const edit = page.getByRole("button", {
      name: "Edit DeepSeek Search",
      exact: true,
    });
    await edit.click();
    const dialog = page.getByRole("dialog");
    const count = dialog.getByLabel("Search results", {
      exact: true,
    });
    const provider = dialog.getByLabel("DeepSeek credential provider", {
      exact: true,
    });
    const save = dialog.getByRole("button", {
      name: "Save changes",
      exact: true,
    });
    await expect(
      dialog.getByRole("button", { name: "Save credential", exact: true }),
    ).toHaveCount(0);
    await expect(
      dialog.getByLabel("Enable plugin globally", { exact: true }),
    ).not.toBeChecked();
    await provider.selectOption("deepseek-search-browser");
    await expect(dialog).toContainText("128 Unicode characters per query");
    await expect(dialog).toContainText("Your bot's model stays unchanged");
    await expect(count).toHaveAttribute("max", "50");
    await expect(count).toHaveAttribute("min", "1");
    await expect(count).toHaveValue("5");
    await count.fill("51");
    expect(
      await count.evaluate(
        (input: HTMLInputElement) => input.validity.rangeOverflow,
      ),
    ).toBe(true);
    await save.click();
    await expect(dialog).toBeVisible();
    expect(
      await (
        await page.request.get("/api/config/plugins/deepseek_search")
      ).json(),
    ).toEqual(before);

    await count.fill("50");
    await save.click();
    await expect(dialog).toHaveCount(0);
    const saved = await (
      await page.request.get("/api/config/plugins/deepseek_search")
    ).json();
    expect(saved.config).toEqual({
      ...before.config,
      provider_id: "deepseek-search-browser",
      count: 50,
    });
    expect(saved.enabled).toBe(false);
    expect(
      await (await page.request.get("/api/config/bots/ada")).json(),
    ).toEqual(botBefore);
    expect(
      await (await page.request.get("/api/config/plugins/web_search")).json(),
    ).toEqual(webSearchBefore);
    expect(
      await (await page.request.get("/api/config/plugins/dumb_search")).json(),
    ).toEqual(parallelBefore);

    await edit.click();
    await expect(provider).toHaveValue("deepseek-search-browser");
    await expect(count).toHaveValue("50");
    await expect(
      dialog.getByLabel("Enable plugin globally", { exact: true }),
    ).not.toBeChecked();
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  } finally {
    const restored = await page.request.post("/api/control", {
      headers: { "X-CSRF-Token": session.csrf },
      data: {
        action: "save",
        kind: "plugins",
        id: "deepseek_search",
        data: { config: before.config },
      },
    });
    expect(restored.ok()).toBeTruthy();
  }
});
