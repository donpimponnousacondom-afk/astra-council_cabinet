import { test, expect } from "@playwright/test";

test("document settings, per-bot budgets and private draft inspection are visible and persist", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByLabel("Dashboard password")
    .fill("test-only-password-never-use-in-production");
  await page.getByRole("button", { name: "Enter council control" }).click();
  await page.getByRole("button", { name: "Plugins", exact: true }).click();
  await page
    .getByRole("button", { name: "Edit Documents & local sites", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByLabel("Local publication base URL", { exact: true }),
  ).toHaveValue("http://127.0.0.1:8000");
  await dialog
    .getByLabel("Planned remote base URL", { exact: true })
    .fill("https://council.example.test");
  await expect(dialog).toContainText("Remote delivery is disabled.");
  await expect(dialog.getByLabel("API key", { exact: true })).toHaveCount(0);
  await expect(
    dialog.getByRole("link", {
      name: "Open local published site",
      exact: true,
    }),
  ).toBeVisible();
  await dialog.getByText("Download draft files (7)", { exact: true }).click();
  const draftLink = dialog
    .getByRole("link", { name: "index.html", exact: true })
    .first();
  const downloaded = await page.request.get(
    (await draftLink.getAttribute("href"))!,
  );
  expect(downloaded.headers()["content-disposition"]).toContain("attachment");
  expect(await downloaded.text()).toContain("PRIVATE DRAFT");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const plugin = await (
    await page.request.get("/api/config/plugins/document_site")
  ).json();
  expect(plugin.config.public_base_url).toBe("https://council.example.test");
  await page.getByRole("button", { name: "Bots", exact: true }).click();
  await page.getByRole("button", { name: "Edit Ada", exact: true }).click();
  await dialog
    .getByRole("button", { name: "Capabilities", exact: true })
    .click();
  await dialog
    .getByRole("checkbox", { name: /^Documents & local sites/ })
    .check();
  await expect(
    dialog.getByLabel("Additional document work rounds", { exact: true }),
  ).toHaveValue("20");
  await dialog
    .getByLabel("Additional document work rounds", { exact: true })
    .fill("25");
  await expect(
    dialog.getByLabel("Document calls allowed in each round", { exact: true }),
  ).toHaveValue("8");
  await expect(
    dialog.getByLabel("Document task time limit (seconds)", { exact: true }),
  ).toHaveValue("900");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const bot = await (await page.request.get("/api/config/bots/ada")).json();
  expect(bot.document_task_rounds).toBe(25);
  expect(bot.enabled_plugins).toContain("document_site");
});
