import { expect, test } from "@playwright/test";

test("Ollama search reuses a provider credential and preserves other search settings", async ({
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
      id: "ollama-search-browser",
      data: {
        name: "Ollama Search Browser",
        base_url: "https://ollama.com/v1",
      },
    },
  });
  expect(created.ok()).toBeTruthy();
  const before = await (
    await page.request.get("/api/config/plugins/web_search")
  ).json();
  const botBefore = await (
    await page.request.get("/api/config/bots/ada")
  ).json();
  await page
    .getByRole("navigation", { name: "Workbench pages" })
    .getByRole("button", { name: "Plugins", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Edit Web search", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Default search engine", { exact: true })
    .selectOption("ollama");
  await dialog
    .getByLabel("Ollama credential provider", { exact: true })
    .selectOption("ollama-search-browser");
  await expect(dialog).toContainText("Your bot's model stays unchanged");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const after = await (
    await page.request.get("/api/config/plugins/web_search")
  ).json();
  expect(after.config).toEqual({
    ...before.config,
    engine: "ollama",
    ollama_provider_id: "ollama-search-browser",
  });
  expect(await (await page.request.get("/api/config/bots/ada")).json()).toEqual(
    botBefore,
  );
  await page
    .getByRole("button", { name: "Edit Web search", exact: true })
    .click();
  await expect(
    dialog.getByLabel("Default search engine", { exact: true }),
  ).toHaveValue("ollama");
  await expect(
    dialog.getByLabel("Ollama credential provider", { exact: true }),
  ).toHaveValue("ollama-search-browser");
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  const restored = await page.request.post("/api/control", {
    headers: { "X-CSRF-Token": session.csrf },
    data: {
      action: "save",
      kind: "plugins",
      id: "web_search",
      data: { config: before.config },
    },
  });
  expect(restored.ok()).toBeTruthy();
});
