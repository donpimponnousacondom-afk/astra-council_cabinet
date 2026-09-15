import { expect, test } from "@playwright/test";

test("prompt library overrides, per-bot toggles and clean slate stay isolated", async ({
  page,
}, testInfo) => {
  await page.goto("/");
  await page
    .getByLabel("Dashboard password")
    .fill("test-only-password-never-use-in-production");
  await page.getByRole("button", { name: "Enter council control" }).click();
  await expect(
    page.getByRole("heading", { name: "The council", exact: true, level: 1 }),
  ).toBeVisible();
  const csrf = (await (await page.request.get("/api/auth/session")).json())
    .csrf;
  const create = await page.request.post("/api/control", {
    headers: { "X-CSRF-Token": csrf },
    data: {
      action: "clone",
      kind: "bots",
      id: "ada",
      data: { id: "prompt-controls-fixture", name: "Prompt controls fixture" },
    },
  });
  expect(create.ok()).toBe(true);
  const otherBefore = await (
    await page.request.get("/api/config/bots/socrates")
  ).json();
  const nav = page.getByRole("navigation", { name: "Workbench pages" });
  await nav
    .getByRole("button", { name: "Prompt library", exact: true })
    .click();
  await page.getByRole("button", { name: "Add prompt", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Display name", { exact: true })
    .fill("Minimal conversation test");
  await dialog
    .getByLabel("Stable identifier", { exact: true })
    .fill("minimal-conversation-test");
  await dialog
    .getByLabel("Prompt placement", { exact: true })
    .selectOption("transcript");
  await dialog.getByLabel("Message role", { exact: true }).selectOption("user");
  await dialog
    .getByLabel("User prompt", { exact: true })
    .fill("{latest_content}");
  await dialog
    .getByRole("button", { name: "Create draft", exact: true })
    .click();
  await expect(dialog).not.toBeVisible();
  await nav.getByRole("button", { name: "Bots", exact: true }).click();
  await page
    .getByRole("button", { name: "Edit Prompt controls fixture", exact: true })
    .click();
  await dialog.getByRole("button", { name: "Prompts", exact: true }).click();
  await dialog
    .getByRole("button", { name: "Disable generated layers" })
    .click();
  await dialog
    .getByLabel("Include Conversation input", { exact: true })
    .check();
  await dialog
    .getByLabel("Template for Conversation input", { exact: true })
    .selectOption("minimal-conversation-test");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).not.toBeVisible();
  const saved = await (
    await page.request.get("/api/config/bots/prompt-controls-fixture")
  ).json();
  expect(saved.disabled_prompt_layers).toContain("identity");
  expect(saved.disabled_prompt_layers).not.toContain("transcript");
  expect(saved.prompt_layer_overrides.transcript).toBe(
    "minimal-conversation-test",
  );
  await page
    .getByRole("button", { name: "Edit Prompt controls fixture", exact: true })
    .click();
  await dialog.getByRole("button", { name: "Prompts", exact: true }).click();
  await expect(
    dialog.getByLabel("Include Identity & response format", { exact: true }),
  ).not.toBeChecked();
  await expect(
    dialog.getByLabel("Template for Conversation input", { exact: true }),
  ).toHaveValue("minimal-conversation-test");
  await dialog.getByRole("button", { name: "Control", exact: true }).click();
  const forget = dialog.getByRole("button", {
    name: "Forget everything before now",
    exact: true,
  });
  await expect(forget).toBeDisabled();
  await dialog
    .getByLabel("Confirm bot ID", { exact: true })
    .fill("prompt-controls-fixture");
  await expect(forget).toBeEnabled();
  page.once("dialog", (d) => d.dismiss());
  await forget.click();
  expect(
    (
      await (
        await page.request.get("/api/config/bots/prompt-controls-fixture")
      ).json()
    ).context_resets,
  ).toEqual([]);
  page.once("dialog", (d) => d.accept());
  await forget.click();
  await expect(dialog.getByRole("status")).toContainText("Clean slate set at");
  await expect(forget).toBeDisabled();
  const reset = await (
    await page.request.get("/api/config/bots/prompt-controls-fixture")
  ).json();
  expect(reset.context_resets[0].channel_id).toBe("*");
  expect(reset.persona).toBe(saved.persona);
  expect(reset.disabled_prompt_layers).toEqual(saved.disabled_prompt_layers);
  expect(
    (await (await page.request.get("/api/config/bots/socrates")).json())
      .context_resets,
  ).toEqual(otherBefore.context_resets);
  await page.screenshot({ path: testInfo.outputPath("bot-control.png") });
});
