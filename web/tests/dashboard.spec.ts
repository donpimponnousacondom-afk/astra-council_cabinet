import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await page
    .getByLabel("Dashboard password")
    .fill("test-only-password-never-use-in-production");
  await page.getByRole("button", { name: "Enter council control" }).click();
  await expect(
    page.getByRole("heading", { name: "The council", exact: true }),
  ).toBeVisible();
});

test("overview, pause/resume, command reference, and mobile navigation", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await expect(
    page.getByText("A few connections. A whole conversation."),
  ).toBeVisible();
  await expect(page.getByText("Draft", { exact: true })).toHaveCount(3);
  await page
    .getByRole("button", { name: "Pause council", exact: true })
    .click();
  await expect(
    page.getByText(
      "The council is paused. Hortator’s commands remain available.",
    ),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Resume council", exact: true })
    .first()
    .click();
  await expect(
    page.getByText(
      "The council is paused. Hortator’s commands remain available.",
    ),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Discord commands", exact: true })
    .click();
  await expect(
    page.getByText("!stop [bot-id | all | providers:id]", { exact: true }),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("button", { name: "Overview", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "The council", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/mobile-overview.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("model profile advanced JSON and personality-preserving switch", async ({
  page,
}) => {
  await page
    .getByRole("button", { name: "Model profiles", exact: true })
    .click();
  await page.getByRole("button", { name: "Add profile", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Display name", { exact: true })
    .fill("UI reasoning profile");
  await dialog
    .getByLabel("Stable identifier", { exact: true })
    .fill("ui-reasoning-profile");
  await dialog
    .getByLabel("Exact model identifier", { exact: true })
    .fill("deepseek/example-model");
  await dialog.getByLabel("Model parameters", { exact: true }).fill(
    JSON.stringify(
      {
        temperature: 0.37,
        reasoning: { effort: "low" },
        provider: { order: ["Example"] },
        custom_vendor: { enabled: true },
      },
      null,
      2,
    ),
  );
  await dialog
    .getByRole("button", { name: "Create draft", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "UI reasoning profile", exact: true }),
  ).toBeVisible();
  const config = await page.request.get(
    "/api/config/profiles/ui-reasoning-profile",
  );
  expect((await config.json()).request_json).toEqual({
    temperature: 0.37,
    reasoning: { effort: "low" },
    provider: { order: ["Example"] },
    custom_vendor: { enabled: true },
  });
  await page.getByRole("button", { name: "Bots", exact: true }).click();
  await page.getByRole("button", { name: "Edit Ada", exact: true }).click();
  await dialog
    .getByRole("button", { name: "Model & rhythm", exact: true })
    .click();
  await dialog
    .getByLabel("Model profile", { exact: true })
    .selectOption("ui-reasoning-profile");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const bot = await (await page.request.get("/api/config/bots/ada")).json();
  expect(bot.model_profile_id).toBe("ui-reasoning-profile");
  expect(bot.persona).toContain("analytical, inventive");
  await page.getByRole("button", { name: "Edit Ada", exact: true }).click();
  await dialog.getByRole("button", { name: "Prompts", exact: true }).click();
  await expect(
    dialog.getByLabel("Personality / system instructions", { exact: true }),
  ).toHaveValue(bot.persona);
  await dialog.getByRole("button", { name: "Close dialog" }).click();
});

test("provider creation, encrypted write-only key, readiness error, and trajectory inspection", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Providers", exact: true }).click();
  await page.getByRole("button", { name: "Add provider", exact: true }).click();
  let dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Display name", { exact: true })
    .fill("UI local provider");
  await dialog
    .getByLabel("Stable identifier", { exact: true })
    .fill("ui-local-provider");
  await dialog
    .getByLabel("API base URL", { exact: true })
    .fill("http://127.0.0.1:11434/v1");
  await dialog
    .getByRole("button", { name: "Create draft", exact: true })
    .click();
  await expect(
    dialog.getByRole("heading", { name: "UI local provider", exact: true }),
  ).toBeVisible();
  await dialog
    .getByLabel("API key", { exact: true })
    .fill("ui-test-only-not-a-real-key");
  await dialog
    .getByRole("button", { name: "Save credential", exact: true })
    .click();
  await expect(dialog.getByLabel("API key", { exact: true })).toHaveValue("");
  await expect(dialog.getByText("Configured", { exact: true })).toBeVisible();
  const provider = await (
    await page.request.get("/api/config/providers/ui-local-provider")
  ).json();
  expect(provider.key_configured).toBe(true);
  expect(JSON.stringify(provider)).not.toContain("ui-test-only-not-a-real-key");
  await dialog.getByRole("button", { name: "Close dialog" }).click();
  await page.getByRole("button", { name: "Bots", exact: true }).click();
  await page
    .getByRole("button", { name: "Edit Socrates", exact: true })
    .click();
  dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "Identity", exact: true }).click();
  await dialog.getByLabel("Activate this bot after saving").check();
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog.getByRole("alert")).toContainText("Bot is not ready");
  await dialog.getByRole("button", { name: "Close dialog" }).click();
  await page.getByRole("button", { name: "Trajectory", exact: true }).click();
  await page.getByRole("button", { name: "Event ledger", exact: true }).click();
  await expect(page.locator(".ledger-event").first()).toBeVisible();
  await page.locator(".ledger-event").first().click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByRole("dialog").locator("pre")).toContainText("actor");
  await page.getByRole("button", { name: "Close dialog" }).click();
  await page.screenshot({
    path: "test-results/trajectory-ledger.png",
    fullPage: true,
  });
});

test("visible reasoning controls preserve vendor JSON, false values, and compaction overrides", async ({
  page,
}) => {
  await page
    .getByRole("button", { name: "Model profiles", exact: true })
    .click();
  await page.getByRole("button", { name: "Add profile", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Display name", { exact: true })
    .fill("Visible reasoning controls");
  await dialog
    .getByLabel("Stable identifier", { exact: true })
    .fill("visible-reasoning-controls");
  await dialog
    .getByLabel("Exact model identifier", { exact: true })
    .fill("vendor/test-model");
  const initial = {
    temperature: 0.37,
    reasoning: { effort: "low", exclude: true },
    chat_template_kwargs: { enable_thinking: true, custom_option: [1, 2] },
    custom_vendor: { thinking_mode: "experimental", unrelated: "preserve me" },
  };
  const raw = dialog.getByLabel("Model parameters", { exact: true });
  await raw.fill(JSON.stringify(initial));
  await dialog
    .getByLabel("Reasoning request field", { exact: true })
    .selectOption("reasoning.effort");
  await expect(
    dialog.getByLabel("Reasoning effort", { exact: true }),
  ).toHaveValue('"low"');
  await dialog
    .getByLabel("Reasoning effort", { exact: true })
    .selectOption('"high"');
  await expect
    .poll(async () => JSON.parse(await raw.inputValue()))
    .toEqual({
      ...initial,
      reasoning: { effort: "high", exclude: true },
    });
  await dialog.getByLabel("Reasoning effort", { exact: true }).selectOption("");
  await expect
    .poll(async () => JSON.parse(await raw.inputValue()).reasoning)
    .toEqual({
      exclude: true,
    });
  await dialog
    .getByLabel("Reasoning effort", { exact: true })
    .selectOption('"medium"');
  await dialog
    .getByLabel("Reasoning request field", { exact: true })
    .selectOption("chat_template_kwargs.enable_thinking");
  await dialog
    .getByLabel("Thinking mode", { exact: true })
    .selectOption("false");
  const expected = {
    ...initial,
    reasoning: { effort: "medium", exclude: true },
    chat_template_kwargs: { enable_thinking: false, custom_option: [1, 2] },
  };
  await expect
    .poll(async () => JSON.parse(await raw.inputValue()))
    .toEqual(expected);
  await dialog
    .getByText("Advanced · compaction parameter overrides", { exact: true })
    .click();
  await dialog
    .getByLabel("Compaction parameters", { exact: true })
    .fill(JSON.stringify({ reasoning: { effort: "minimal" } }));
  const preview = dialog.locator("details.code-block").filter({
    has: page.locator("summary", {
      hasText: "Effective compaction reasoning fields",
    }),
  });
  await preview.locator("summary").click();
  expect(JSON.parse(await preview.locator("pre").innerText())).toEqual({
    reasoning: { effort: "minimal" },
    chat_template_kwargs: { enable_thinking: false },
    custom_vendor: { thinking_mode: "experimental" },
  });
  await dialog
    .getByRole("button", { name: "Create draft", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const profile = await (
    await page.request.get("/api/config/profiles/visible-reasoning-controls")
  ).json();
  expect(profile.request_json).toEqual(expected);
  expect(profile.compaction_request_json).toEqual({
    reasoning: { effort: "minimal" },
  });
  const card = page.locator(".catalog-card").filter({
    has: page.getByRole("heading", {
      name: "Visible reasoning controls",
      exact: true,
    }),
  });
  await expect(card).toContainText('"enable_thinking":false');
  await expect(card).toContainText("experimental");
  await expect(card).toContainText("Not specified");
  await card.getByRole("button", { name: "Edit profile" }).click();
  await expect
    .poll(async () => JSON.parse(await raw.inputValue()))
    .toEqual(expected);
  await page.setViewportSize({ width: 390, height: 844 });
  await dialog
    .getByRole("region", { name: "Reasoning configuration" })
    .scrollIntoViewIfNeeded();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/reasoning-controls-mobile.png",
    fullPage: true,
  });
  await dialog.getByRole("button", { name: "Close dialog" }).click();
});

test("reasoning controls protect invalid JSON and custom structures and distinguish unset from off", async ({
  page,
}) => {
  await page
    .getByRole("button", { name: "Model profiles", exact: true })
    .click();
  await page.getByRole("button", { name: "Add profile", exact: true }).click();
  const dialog = page.getByRole("dialog");
  const raw = dialog.getByLabel("Model parameters", { exact: true });
  const field = dialog.getByLabel("Reasoning request field", { exact: true });
  await raw.fill('{"vendor_option": 42}');
  await field.selectOption("reasoning.effort");
  await expect(
    dialog.getByLabel("Reasoning effort", { exact: true }),
  ).toHaveValue("");
  await dialog
    .getByLabel("Reasoning effort", { exact: true })
    .selectOption('"low"');
  await dialog.getByLabel("Reasoning effort", { exact: true }).selectOption("");
  await expect
    .poll(async () => JSON.parse(await raw.inputValue()))
    .toEqual({ vendor_option: 42 });
  await raw.fill(
    '{"vendor_option":42,"chat_template_kwargs":{"thinking_budget":-1}}',
  );
  await field.selectOption("chat_template_kwargs.thinking_budget");
  const budget = dialog.getByLabel("Reasoning token budget", { exact: true });
  await expect(budget).toHaveValue("-1");
  expect(
    await budget.evaluate((input: HTMLInputElement) => input.validity.valid),
  ).toBe(true);
  await field.selectOption("reasoning.effort");
  await raw.fill('{"vendor_option":');
  await expect(field).toBeDisabled();
  await expect(
    dialog.getByLabel("Reasoning effort", { exact: true }),
  ).toBeDisabled();
  await expect(raw).toHaveValue('{"vendor_option":');
  await raw.fill(
    '{"vendor_option":42,"reasoning":"custom-mode","thinking":{"type":"adaptive","vendor_budget":123}}',
  );
  await expect(field).toBeEnabled();
  await expect(
    dialog.getByLabel("Reasoning effort", { exact: true }),
  ).toBeDisabled();
  await field.selectOption("thinking");
  await expect(
    dialog.getByLabel("Thinking mode", { exact: true }),
  ).toBeDisabled();
  await field.selectOption("thinking.type");
  await expect(dialog.getByLabel("Thinking mode", { exact: true })).toHaveValue(
    '"adaptive"',
  );
  await dialog
    .getByLabel("Thinking mode", { exact: true })
    .selectOption('"disabled"');
  await expect
    .poll(async () => JSON.parse(await raw.inputValue()).thinking)
    .toEqual({
      type: "disabled",
      vendor_budget: 123,
    });
  await field.selectOption("thinking.budget_tokens");
  await dialog
    .getByLabel("Reasoning token budget", { exact: true })
    .fill("1024");
  await expect
    .poll(async () => JSON.parse(await raw.inputValue()).thinking)
    .toEqual({
      type: "disabled",
      vendor_budget: 123,
      budget_tokens: 1024,
    });
  await dialog.getByLabel("Reasoning token budget", { exact: true }).fill("");
  await expect
    .poll(async () => JSON.parse(await raw.inputValue()).thinking)
    .toEqual({
      type: "disabled",
      vendor_budget: 123,
    });
  await raw.fill('{"thinking":false,"vendor_option":42}');
  await field.selectOption("thinking");
  await expect(dialog.getByLabel("Thinking mode", { exact: true })).toHaveValue(
    "false",
  );
  await dialog.getByLabel("Thinking mode", { exact: true }).selectOption("");
  await expect
    .poll(async () => JSON.parse(await raw.inputValue()))
    .toEqual({ vendor_option: 42 });
  await dialog.getByRole("button", { name: "Close dialog" }).click();
});

test("all operational pages render without errors", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  for (const name of [
    "Analytics",
    "Prompt library",
    "Plugins",
    "Rooms",
    "Council settings",
  ]) {
    await page.getByRole("button", { name, exact: true }).click();
    await expect(page.locator("main h1")).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  }
  await page.getByRole("button", { name: "Overview", exact: true }).click();
  await page.screenshot({
    path: "test-results/desktop-overview.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("populated trajectory shows exact requests, tool results, delivery, and compaction", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Trajectory", exact: true }).click();
  await expect(page.locator(".turn-row")).toHaveCount(2);
  await page.locator(".turn-row").filter({ hasText: "sent" }).click();
  const inspector = page.locator(".turn-inspector");
  await expect(
    inspector.getByRole("heading", { name: "Ada", exact: true }),
  ).toBeVisible();
  await inspector
    .getByRole("button", { name: "requests", exact: true })
    .click();
  await expect(inspector.locator(".request-detail")).toHaveCount(2);
  await inspector
    .getByText("Exact request body (secrets and reasoning redacted)", {
      exact: true,
    })
    .first()
    .click();
  await expect(inspector.locator("pre").first()).toContainText(
    "Browser verification fixture",
  );
  await inspector.getByRole("button", { name: "tools", exact: true }).click();
  await expect(inspector).toContainText(
    "Inspect the timeline for observability.",
  );
  await inspector
    .getByRole("button", { name: "delivery", exact: true })
    .click();
  await expect(inspector).toContainText("666666666666666666");
  await expect(inspector).toContainText("Synthetic test output");
  const downloaded = page.waitForEvent("download");
  await inspector.getByRole("link", { name: "Export full trajectory" }).click();
  expect((await downloaded).suggestedFilename()).toMatch(/^turn_.*\.json$/);
  await page.getByRole("button", { name: "Close turn inspector" }).click();
  await page
    .locator(".turn-row")
    .filter({ hasText: "manual compaction" })
    .click();
  await inspector.getByRole("button", { name: "context", exact: true }).click();
  await expect(
    inspector.getByText("compaction.completed", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/populated-trajectory.png",
    fullPage: true,
  });
});
