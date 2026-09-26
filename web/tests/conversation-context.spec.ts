import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

async function login(page: Page) {
  await page.goto("/");
  await page
    .getByLabel("Dashboard password")
    .fill("test-only-password-never-use-in-production");
  await page.getByRole("button", { name: "Enter council control" }).click();
  await expect(
    page.getByRole("heading", { name: "The council", exact: true }),
  ).toBeVisible();
}

async function navigate(page: Page, name: string) {
  await page
    .getByRole("navigation", { name: "Workbench pages" })
    .getByRole("button", { name, exact: true })
    .click();
}

async function createBot(page: Page, id: string, extra: object = {}) {
  const session = await (await page.request.get("/api/auth/session")).json();
  const response = await page.request.post("/api/control", {
    headers: { "X-CSRF-Token": session.csrf },
    data: {
      action: "create",
      kind: "bots",
      id,
      data: {
        id,
        name: id,
        enabled: false,
        model_profile_id: "balanced",
        enabled_plugins: [],
        ...extra,
      },
    },
  });
  expect(response.ok(), await response.text()).toBe(true);
  await page.getByRole("button", { name: "Refresh dashboard" }).click();
  return response.json();
}

test("conversation format selects matching prompt variants and preserves drafts and other bots", async ({
  page,
}, testInfo) => {
  await login(page);
  const id = "conversation-format-fixture";
  const before = await createBot(page, id, {
    persona: "Keep this persona",
    disabled_prompt_layers: ["runtime_facts"],
    plugin_config: {
      document_site: { local_base_url: "http://localhost:19001" },
    },
  });
  const other = await (await page.request.get("/api/config/bots/ada")).json();
  await navigate(page, "Bots");
  await page.getByRole("button", { name: `Edit ${id}`, exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "Prompts", exact: true }).click();
  const format = dialog.getByLabel("Conversation format", { exact: true });
  await expect(format).toHaveValue("structured");
  await dialog.locator('[data-layer="transcript"] button').click();
  await expect(
    dialog.getByText("Template text · runtime-transcript", { exact: true }),
  ).toBeVisible();
  await dialog.locator('[data-layer="compaction_instructions"] button').click();
  await expect(
    dialog.getByText("Template text · runtime-compaction-instructions", {
      exact: true,
    }),
  ).toBeVisible();
  await format.selectOption("conversation");
  await dialog.locator('[data-layer="transcript"] button').click();
  await expect(
    dialog.getByText("Template text · runtime-transcript-conversation", {
      exact: true,
    }),
  ).toBeVisible();
  await dialog.locator('[data-layer="compaction_instructions"] button').click();
  await expect(
    dialog.getByText(
      "Template text · runtime-compaction-instructions-conversation",
      { exact: true },
    ),
  ).toBeVisible();
  await page.getByRole("button", { name: "Refresh dashboard" }).click();
  await expect(format).toHaveValue("conversation");
  await dialog.getByRole("button", { name: "Identity", exact: true }).click();
  await dialog.getByRole("button", { name: "Prompts", exact: true }).click();
  await expect(format).toHaveValue("conversation");
  await dialog
    .getByLabel("Template for Conversation input", { exact: true })
    .selectOption("runtime-transcript");
  await dialog.locator('[data-layer="transcript"] button').click();
  await expect(
    dialog.getByText("Template text · runtime-transcript", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByText("Template text · runtime-transcript-conversation", {
      exact: true,
    }),
  ).toHaveCount(0);
  await dialog
    .getByLabel("Template for Conversation input", { exact: true })
    .selectOption("");
  await page.screenshot({
    path: testInfo.outputPath("conversation-template.png"),
  });
  await format.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: testInfo.outputPath("conversation-format.png"),
  });
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const saved = await (await page.request.get(`/api/config/bots/${id}`)).json();
  expect(saved.transcript_format).toBe("conversation");
  for (const key of [
    "persona",
    "model_profile_id",
    "enabled",
    "enabled_plugins",
    "plugin_config",
    "disabled_prompt_layers",
    "prompt_layer_overrides",
  ])
    expect(saved[key]).toEqual(before[key]);
  expect(await (await page.request.get("/api/config/bots/ada")).json()).toEqual(
    other,
  );
  await page.getByRole("button", { name: `Edit ${id}`, exact: true }).click();
  await dialog.getByRole("button", { name: "Prompts", exact: true }).click();
  await expect(format).toHaveValue("conversation");
});

test("engram configuration stays opt-in and per-bot overrides inherit cleanly", async ({
  page,
}, testInfo) => {
  await login(page);
  const global = await (
    await page.request.get("/api/config/plugins/engram")
  ).json();
  expect(global.enabled).toBe(false);
  await navigate(page, "Plugins");
  await page
    .getByRole("button", { name: `Edit ${global.name}`, exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  const chars = dialog.getByLabel("Engram memory limit (characters)", {
    exact: true,
  });
  const tokens = dialog.getByLabel("Engram memory limit (tokens)", {
    exact: true,
  });
  const recent = dialog.getByLabel("Recent acknowledged messages", {
    exact: true,
  });
  await expect(chars).toHaveValue("8000");
  await expect(tokens).toHaveValue("2048");
  await expect(recent).toHaveValue("12");
  await expect(
    dialog.getByLabel("Reduce history after acknowledged engrams", {
      exact: true,
    }),
  ).not.toBeChecked();
  await expect(dialog).toContainText("all new or uncovered messages");
  for (const [input, invalid] of [
    [chars, ""],
    [chars, "128001"],
    [tokens, "32001"],
    [recent, "0"],
  ] as const) {
    const original = await input.inputValue();
    await input.fill(invalid);
    await dialog
      .getByRole("button", { name: "Save changes", exact: true })
      .click();
    expect(
      await input.evaluate((el: HTMLInputElement) => el.checkValidity()),
    ).toBe(false);
    expect(
      await (await page.request.get("/api/config/plugins/engram")).json(),
    ).toEqual(global);
    await input.fill(original);
  }
  await chars.fill("9000");
  await tokens.fill("2300");
  await recent.fill("16");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const savedGlobal = await (
    await page.request.get("/api/config/plugins/engram")
  ).json();
  expect(savedGlobal.enabled).toBe(false);
  expect(savedGlobal.config).toMatchObject({
    state_char_limit: 9000,
    state_token_limit: 2300,
    recent_messages: 16,
    reduce_history: false,
  });
  const id = "engram-overrides-fixture";
  const overrides = {
    document_site: { local_base_url: "http://localhost:19001" },
  };
  const before = await createBot(page, id, {
    enabled_plugins: ["engram"],
    plugin_config: overrides,
  });
  await navigate(page, "Bots");
  const openCapabilities = async () => {
    await page.getByRole("button", { name: `Edit ${id}`, exact: true }).click();
    await dialog
      .getByRole("button", { name: "Capabilities", exact: true })
      .click();
  };
  await openCapabilities();
  await expect(chars).toHaveValue("");
  await expect(chars).toHaveAttribute("placeholder", "Inherit 9000");
  await expect(tokens).toHaveAttribute("placeholder", "Inherit 2300");
  await expect(recent).toHaveAttribute("placeholder", "Inherit 16");
  const reduction = dialog.getByLabel("History reduction for this bot", {
    exact: true,
  });
  await expect(reduction).toHaveValue("");
  await chars.fill("6000");
  await tokens.fill("1000");
  await recent.fill("12");
  await reduction.selectOption("true");
  await dialog.getByRole("button", { name: "Identity", exact: true }).click();
  await dialog
    .getByRole("button", { name: "Capabilities", exact: true })
    .click();
  await expect(reduction).toHaveValue("true");
  await expect(recent).toHaveValue("12");
  await chars.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("engram-overrides.png") });
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const saved = await (await page.request.get(`/api/config/bots/${id}`)).json();
  expect(saved.plugin_config).toEqual({
    ...overrides,
    engram: {
      state_char_limit: 6000,
      state_token_limit: 1000,
      recent_messages: 12,
      reduce_history: true,
    },
  });
  for (const key of [
    "enabled",
    "enabled_plugins",
    "model_profile_id",
    "transcript_format",
  ])
    expect(saved[key]).toEqual(before[key]);
  await openCapabilities();
  await chars.fill("");
  await tokens.fill("");
  await recent.fill("");
  await reduction.selectOption("");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(
    (await (await page.request.get(`/api/config/bots/${id}`)).json())
      .plugin_config,
  ).toEqual({ ...overrides, engram: {} });
  expect(
    await (await page.request.get("/api/config/plugins/engram")).json(),
  ).toEqual(savedGlobal);
});

test("engram inspection remains read-only while disabled and reset needs an explicit scope and confirmation", async ({
  page,
}, testInfo) => {
  await login(page);
  const id = "engram-state-fixture";
  const before = await createBot(page, id, { persona: "Saved persona" });
  let loads = 0;
  let resets = 0;
  let cleared = false;
  const requests: object[] = [];
  // Saved state and reset failures are synthetic. No provider or Discord call starts.
  await page.route(`**/api/engrams/${id}`, async (route) => {
    loads += 1;
    await route.fulfill({
      json: {
        bot_id: id,
        enabled: false,
        ordinary_only: true,
        config: { reduce_history: false },
        pending_candidates: 0,
        states: [
          {
            channel_id: "1001",
            revision: cleared ? 2 : 1,
            epoch: "0:0",
            covered_through: cleared ? 0 : 14,
            summary_checkpoint: 8,
            summary_hash: "fixture-summary-hash",
            MEM: cleared ? "" : "A remembered commitment",
            FACTS: cleared ? "" : "A confirmed fact",
            state_chars: cleared ? 0 : 39,
            state_tokens: cleared ? 0 : 8,
            updated_at: 1790000000,
            source_turn_id: "turn-fixture",
            source_request_id: "request-fixture",
          },
        ],
      },
    });
  });
  await page.route(`**/api/engrams/${id}/reset`, async (route) => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().headers()["x-csrf-token"]).toBeTruthy();
    resets += 1;
    requests.push(route.request().postDataJSON());
    if (resets === 1) {
      await route.fulfill({
        status: 409,
        json: { error: "Fixture reset unavailable" },
      });
    } else {
      cleared = true;
      await route.fulfill({
        json: { bot_id: id, channel_id: "1001", reset: true },
      });
    }
  });
  await navigate(page, "Bots");
  await page.getByRole("button", { name: `Edit ${id}`, exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "Control", exact: true }).click();
  expect(loads).toBe(0);
  await dialog
    .getByText("Engram memory · experimental", { exact: true })
    .click();
  const panel = dialog.getByRole("region", { name: "Engram state inspection" });
  await expect(panel).toContainText("Disabled · 0 pending candidates");
  expect(loads).toBe(1);
  expect(resets).toBe(0);
  await panel.getByText("MEM", { exact: true }).click();
  await panel.getByText("FACTS", { exact: true }).click();
  await expect(panel).toContainText("A remembered commitment");
  await expect(panel).toContainText("A confirmed fact");
  await panel.getByText("Engram coverage and source", { exact: true }).click();
  await expect(panel).toContainText("request-fixture");
  const reset = panel.getByRole("button", {
    name: "Reset engram memory",
    exact: true,
  });
  await expect(reset).toBeDisabled();
  await panel.getByLabel("Confirm engram reset bot ID").fill(id);
  await expect(reset).toBeEnabled();
  await panel.getByLabel("Engram scope").selectOption("1001");
  await expect(reset).toBeDisabled();
  await panel.getByLabel("Confirm engram reset bot ID").fill(id);
  page.once("dialog", (d) => d.dismiss());
  await reset.click();
  expect(resets).toBe(0);
  await page.screenshot({ path: testInfo.outputPath("engram-state.png") });
  page.once("dialog", (d) => d.accept());
  await reset.click();
  await expect(panel.getByRole("alert")).toHaveText(
    "Fixture reset unavailable",
  );
  page.once("dialog", (d) => d.accept());
  await reset.click();
  await expect(panel.getByRole("status")).toHaveText(
    "Engram memory reset for 1001 (1001).",
  );
  await expect(panel).not.toContainText("A remembered commitment");
  await expect(reset).toBeDisabled();
  expect(requests).toEqual([
    { confirm_bot_id: id, channel_id: "1001" },
    { confirm_bot_id: id, channel_id: "1001" },
  ]);
  expect(
    await (await page.request.get(`/api/config/bots/${id}`)).json(),
  ).toEqual(before);
  await dialog.getByRole("button", { name: "Prompts", exact: true }).click();
  await dialog
    .getByLabel("Personality / system instructions", { exact: true })
    .fill("Unsaved persona");
  await dialog.getByRole("button", { name: "Control", exact: true }).click();
  await dialog
    .getByText("Engram memory · experimental", { exact: true })
    .click();
  await panel.getByLabel("Confirm engram reset bot ID").fill(id);
  await expect(reset).toBeDisabled();
  await expect(panel).toContainText(
    "Save or discard configuration drafts before resetting engrams.",
  );
});
