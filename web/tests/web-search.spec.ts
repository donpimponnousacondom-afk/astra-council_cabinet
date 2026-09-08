import { test, expect } from "@playwright/test";

test("web search engine settings and write-only Brave credential persist", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByLabel("Dashboard password")
    .fill("test-only-password-never-use-in-production");
  await page.getByRole("button", { name: "Enter council control" }).click();
  await page.getByRole("button", { name: "Plugins", exact: true }).click();
  await page
    .getByRole("button", { name: "Edit Web search", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  const before = await (
    await page.request.get("/api/config/plugins/web_search")
  ).json();
  await expect(
    dialog.getByLabel("Default search engine", { exact: true }),
  ).toHaveValue("auto");
  await expect(
    dialog.getByLabel("Search results per engine", { exact: true }),
  ).toHaveValue("5");
  await expect(dialog).toContainText("DuckDuckGo needs no API key");
  await expect(
    dialog.getByRole("link", { name: "Brave API dashboard", exact: true }),
  ).toHaveAttribute("href", "https://api-dashboard.search.brave.com/");
  await dialog
    .getByLabel("Default search engine", { exact: true })
    .selectOption("both");
  await dialog
    .getByLabel("Search results per engine", { exact: true })
    .fill("7");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await page
    .getByRole("button", { name: "Edit Web search", exact: true })
    .click();
  await expect(
    dialog.getByLabel("Default search engine", { exact: true }),
  ).toHaveValue("both");
  await expect(
    dialog.getByLabel("Search results per engine", { exact: true }),
  ).toHaveValue("7");
  const after = await (
    await page.request.get("/api/config/plugins/web_search")
  ).json();
  expect(after.config).toEqual({ ...before.config, engine: "both", count: 7 });
  const fixtureKey = "synthetic-brave-browser-key";
  await dialog.getByLabel("API key", { exact: true }).fill(fixtureKey);
  await dialog
    .getByRole("button", { name: "Save credential", exact: true })
    .click();
  await expect(dialog.getByLabel("API key", { exact: true })).toHaveValue("");
  const saved = await (
    await page.request.get("/api/config/plugins/web_search")
  ).json();
  expect(saved.key_configured).toBe(true);
  expect(JSON.stringify(saved)).not.toContain(fixtureKey);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    dialog.getByLabel("Default search engine", { exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
});
