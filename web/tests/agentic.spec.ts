import { test, expect } from "@playwright/test";

test("keyless workspace and reading controls persist with private inspection", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByLabel("Dashboard password")
    .fill("test-only-password-never-use-in-production");
  await page.getByRole("button", { name: "Enter council control" }).click();
  await page.getByRole("button", { name: "Plugins", exact: true }).click();
  await page
    .getByRole("button", { name: "Edit Private workspaces", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByLabel("Workspace file limit (bytes)", { exact: true }),
  ).toHaveValue("33554432");
  await expect(dialog.getByLabel("API key", { exact: true })).toHaveCount(0);
  await dialog
    .getByLabel("Returned file chunk (bytes)", { exact: true })
    .fill("8000");
  await expect(
    dialog.getByText(/Isolated runner ready|Runner unavailable/, {
      exact: true,
    }),
  ).toBeVisible();
  await dialog
    .getByRole("button", { name: "Inspect files", exact: true })
    .first()
    .click();
  await dialog.getByRole("button", { name: /notes.txt \(/ }).click();
  await expect(
    dialog.getByText(/PRIVATE WORKSPACE NOTES/, { exact: false }).last(),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "Next chunk", exact: true }).click();
  await expect(dialog.locator(".agent-tool-inspection pre")).toContainText(
    "Unicode café",
  );
  await expect(dialog.locator(".agent-tool-inspection pre")).not.toContainText(
    "PRIVATE WORKSPACE NOTES",
  );
  await dialog
    .getByRole("button", { name: "Inspect text", exact: true })
    .first()
    .click();
  await expect(dialog.locator(".agent-tool-inspection pre")).toContainText(
    "PUBLIC FIXTURE SNAPSHOT",
  );
  await dialog
    .getByRole("button", { name: "Inspect job", exact: true })
    .click();
  await expect(
    dialog.getByText("succeeded · 26,047 output bytes"),
  ).toBeVisible();
  await dialog
    .getByRole("button", { name: "Read stdout", exact: true })
    .click();
  await expect(dialog.locator(".agent-tool-inspection pre")).toContainText(
    "SYNTHETIC JOB OUTPUT",
  );
  await dialog.getByRole("button", { name: "Next chunk", exact: true }).click();
  await expect(dialog.locator(".agent-tool-inspection pre")).toContainText(
    "Unicode café",
  );
  await expect(dialog.locator(".agent-tool-inspection pre")).not.toContainText(
    "SYNTHETIC JOB OUTPUT",
  );
  await dialog
    .getByRole("button", { name: "Inspect job", exact: true })
    .click();
  await dialog
    .getByRole("button", { name: "Read stderr", exact: true })
    .click();
  await expect(dialog.locator(".agent-tool-inspection pre")).toContainText(
    "Synthetic diagnostic line",
  );
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const plugin = await (
    await page.request.get("/api/config/plugins/workspace")
  ).json();
  expect(plugin.config.max_read_bytes).toBe(8000);
  expect(plugin.keyless).toBe(true);
  await page.getByRole("button", { name: "Bots", exact: true }).click();
  await page.getByRole("button", { name: "Edit Ada", exact: true }).click();
  await dialog
    .getByRole("button", { name: "Capabilities", exact: true })
    .click();
  await dialog.getByRole("checkbox", { name: /^Private workspaces/ }).check();
  await dialog.getByRole("checkbox", { name: /^Isolated Bash/ }).check();
  await expect(
    dialog.getByLabel("Additional file and reading rounds", { exact: true }),
  ).toHaveValue("20");
  await dialog
    .getByLabel("Additional file and reading rounds", { exact: true })
    .fill("24");
  await dialog
    .getByLabel("Active tool context (estimated tokens)", { exact: true })
    .fill("4800");
  await page.setViewportSize({ width: 390, height: 844 });
  const box = await dialog
    .getByRole("checkbox", { name: /^Private workspaces/ })
    .boundingBox();
  expect(box?.width).toBeGreaterThanOrEqual(16);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await dialog
    .getByLabel("Additional file and reading rounds", { exact: true })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: "test-results/agentic-mobile.png",
    fullPage: true,
  });
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const bot = await (await page.request.get("/api/config/bots/ada")).json();
  expect(bot.work_task_rounds).toBe(24);
  expect(bot.tool_working_set_tokens).toBe(4800);
  expect(bot.enabled_plugins).toContain("shell");
});

test("web fetch distinguishes download bytes, returned characters, storage and retention", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByLabel("Dashboard password")
    .fill("test-only-password-never-use-in-production");
  await page.getByRole("button", { name: "Enter council control" }).click();
  await page.getByRole("button", { name: "Plugins", exact: true }).click();
  await page
    .getByRole("button", { name: "Edit Web fetch", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByLabel("Download limit (bytes)", { exact: true }),
  ).toHaveValue("1000000");
  await expect(
    dialog.getByLabel("Returned chunk (Unicode characters)", { exact: true }),
  ).toHaveValue("18000");
  await expect(
    dialog.getByLabel("Fetched text storage per bot (bytes)", { exact: true }),
  ).toHaveValue("50000000");
  await expect(
    dialog.getByLabel("Snapshot retention (seconds)", { exact: true }),
  ).toHaveValue("604800");
  await expect(dialog.getByLabel("API key", { exact: true })).toHaveCount(0);
  await dialog
    .getByLabel("Returned chunk (Unicode characters)", { exact: true })
    .fill("9000");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await page
    .getByRole("button", { name: "Edit Web fetch", exact: true })
    .click();
  await expect(
    dialog.getByLabel("Returned chunk (Unicode characters)", { exact: true }),
  ).toHaveValue("9000");
});
