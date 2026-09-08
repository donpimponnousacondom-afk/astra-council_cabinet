import { test, expect } from "@playwright/test";

test("Hortator control scope and external application intake are explained without changing configuration", async ({
  page,
}) => {
  const before = await (
    await page.request.get("/api/config/settings/global")
  ).json();
  await page
    .getByRole("button", { name: "Council settings", exact: true })
    .click();
  await expect(
    page.getByText("Hortator control channel", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Edit council settings", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByLabel("Hortator control channel ID", { exact: true }),
  ).toHaveValue(before.control_channel_id);
  await expect(dialog).toContainText(
    "Mentions in other server channels do not bypass that scope.",
  );
  await dialog.getByRole("button", { name: "Close dialog" }).click();
  await page.getByRole("button", { name: "Rooms", exact: true }).click();
  await page
    .getByRole("button", { name: "Edit The council", exact: true })
    .click();
  await expect(dialog).toContainText("including slash-command results");
  await expect(dialog).toContainText(
    "Ordinary incoming webhooks remain excluded",
  );
  expect(
    await (await page.request.get("/api/config/settings/global")).json(),
  ).toEqual(before);
  const plugin = await (
    await page.request.get("/api/config/plugins/discord_send")
  ).json();
  expect(plugin.keyless).toBe(true);
});

test("SSE is an immediate per-model card option with matching editor and preserved parameters", async ({
  page,
}) => {
  await page
    .getByRole("button", { name: "Model profiles", exact: true })
    .click();
  await page.getByRole("button", { name: "Add profile", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Display name", { exact: true })
    .fill("SSE fixture profile");
  await dialog
    .getByLabel("Stable identifier", { exact: true })
    .fill("sse-fixture-profile");
  await dialog
    .getByLabel("Exact model identifier", { exact: true })
    .fill("fixture-model");
  const parameters = {
    temperature: 0.4,
    vendor: { custom: true },
    stream_options: { include_usage: true },
  };
  await dialog
    .getByLabel("Model parameters", { exact: true })
    .fill(JSON.stringify(parameters));
  await expect(
    dialog.getByRole("checkbox", { name: "SSE streaming", exact: true }),
  ).toBeChecked();
  await dialog
    .getByRole("button", { name: "Create draft", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const card = page.locator(".catalog-card").filter({
    has: page.getByRole("heading", {
      name: "SSE fixture profile",
      exact: true,
    }),
  });
  const getProfile = async () =>
    (await page.request.get("/api/config/profiles/sse-fixture-profile")).json();
  const providersBefore = await (
    await page.request.get("/api/config/providers")
  ).json();
  const before = await getProfile();
  const toggle = card.getByRole("checkbox", {
    name: "SSE streaming",
    exact: true,
  });
  await expect(toggle).toBeChecked();
  await toggle.click();
  await expect(toggle).not.toBeChecked();
  await expect.poll(async () => (await getProfile()).stream).toBe(false);
  const after = await getProfile();
  expect(after.revision).toBe(before.revision + 1);
  expect(after.request_json).toEqual(parameters);
  expect(after.include_usage).toBe(before.include_usage);
  expect(after.provider_id).toBe(before.provider_id);
  expect(
    await (await page.request.get("/api/config/providers")).json(),
  ).toEqual(providersBefore);
  await expect(card).toContainText("Complete JSON response");
  await card.getByRole("button", { name: "Edit profile", exact: true }).click();
  await expect(
    dialog.getByRole("checkbox", { name: "SSE streaming", exact: true }),
  ).not.toBeChecked();
  await expect(
    dialog.getByRole("checkbox", {
      name: "Request stream usage data",
      exact: true,
    }),
  ).toBeDisabled();
  await dialog
    .getByRole("checkbox", { name: "SSE streaming", exact: true })
    .click();
  await expect(
    dialog.getByRole("checkbox", {
      name: "Request stream usage data",
      exact: true,
    }),
  ).toBeEnabled();
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(toggle).toBeChecked();
  expect((await getProfile()).request_json).toEqual(parameters);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(toggle).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("trajectory explains missing private capture for the selected request", async ({
  page,
}) => {
  await page.route("**/api/trajectory/*", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    if (body.requests) {
      body.requests[0].diagnostics = {
        capture: "unavailable",
        reasoning_status: "not_recorded",
        request_id: body.requests[0].id,
        note: "No private diagnostic capture is stored for this request. Whether its provider returned reasoning is unknown.",
      };
    }
    await route.fulfill({ json: body });
  });
  await page.getByRole("button", { name: "Trajectory", exact: true }).click();
  await page.locator(".turn-row").filter({ hasText: "sent" }).click();
  const inspector = page.locator(".turn-inspector");
  await inspector
    .getByRole("button", { name: "requests", exact: true })
    .click();
  const diagnostic = inspector
    .locator("details")
    .filter({ hasText: "Provider reasoning & diagnostics" })
    .first();
  await diagnostic.locator("summary").click();
  await expect(diagnostic).toContainText(
    "Whether its provider returned reasoning is unknown.",
  );
  await expect(diagnostic).not.toContainText("Older discarded reasoning");
  await expect(diagnostic.locator("pre")).toContainText("not_recorded");
});

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

test("running version shows real server and dashboard identities with ISO dates", async ({
  page,
}) => {
  const version = await (await page.request.get("/api/version")).json();
  const build = await (await page.request.get("/build-info.json")).json();
  const banner = page.getByRole("region", { name: "Running version" });
  await expect(banner).toContainText(version.short_commit);
  await expect(banner).toContainText(version.commit_title);
  await expect(banner.locator("time")).toHaveText(version.committed_at);
  expect(version.committed_at).toMatch(
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/,
  );
  await banner.getByText("Build details", { exact: true }).click();
  await expect(banner).toContainText(version.started_at);
  const row = (name: string) =>
    banner
      .locator("dl > div")
      .filter({ has: page.getByText(name, { exact: true }) })
      .locator("dd");
  await expect(row("Server commit")).toHaveText(version.commit);
  await expect(row("Dashboard commit")).toHaveText(build.commit);
  await expect(row("Dashboard commit title")).toHaveText(build.commit_title);
  await expect(row("Dashboard built")).toHaveText(build.built_at);
  await page.screenshot({
    path: "test-results/running-version-desktop.png",
    animations: "disabled",
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(banner).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/running-version-mobile.png",
    animations: "disabled",
  });
});

test("version distinguishes different commits, uncommitted source and missing metadata", async ({
  page,
}) => {
  const actual = await (await page.request.get("/api/version")).json();
  let reported: typeof actual | undefined = {
    ...actual,
    commit: "f".repeat(40),
    short_commit: "f".repeat(12),
    commit_title: "Alternate running build",
    dirty: true,
  };
  await page.route("**/api/status", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ json: { ...body, version: reported } });
  });
  await page.getByRole("button", { name: "Refresh dashboard" }).click();
  const banner = page.getByRole("region", { name: "Running version" });
  await expect(banner).toContainText("Alternate running build");
  await expect(banner).toContainText("Uncommitted changes at startup");
  await expect(banner).toContainText(
    "The dashboard and server are from different commits.",
  );
  reported = undefined;
  await page.getByRole("button", { name: "Refresh dashboard" }).click();
  await expect(banner).toContainText("Commit unavailable");
  await expect(banner).toContainText(
    "Commit metadata was not supplied by this server.",
  );
  await expect(banner.getByRole("status")).toHaveCount(0);
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

test("provider User-Agent round-trips with advanced headers and upstream errors preserve the dashboard session", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Providers", exact: true }).click();
  await page.getByRole("button", { name: "Add provider", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Display name", { exact: true })
    .fill("UI User Agent");
  await dialog
    .getByLabel("Stable identifier", { exact: true })
    .fill("ui-user-agent");
  await dialog
    .getByLabel("API base URL", { exact: true })
    .fill("https://provider.test/v1");
  await dialog
    .getByText("Advanced · non-secret HTTP headers", { exact: true })
    .click();
  await dialog
    .getByLabel("Request headers", { exact: true })
    .fill(
      JSON.stringify({ "uSeR-aGeNt": "Initial/1.0", "X-Title": "Preserved" }),
    );
  await expect(dialog.getByLabel("User-Agent", { exact: true })).toHaveValue(
    "Initial/1.0",
  );
  await dialog
    .getByLabel("User-Agent", { exact: true })
    .fill("Council Client/2.0");
  await dialog
    .getByRole("button", { name: "Create draft", exact: true })
    .click();
  await expect(
    dialog.getByRole("heading", { name: "UI User Agent", exact: true }),
  ).toBeVisible();
  const saved = await (
    await page.request.get("/api/config/providers/ui-user-agent")
  ).json();
  expect(saved.headers).toEqual({
    "User-Agent": "Council Client/2.0",
    "X-Title": "Preserved",
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await dialog
    .getByLabel("User-Agent", { exact: true })
    .scrollIntoViewIfNeeded();
  await expect(dialog.getByLabel("User-Agent", { exact: true })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/provider-user-agent-mobile.png",
    animations: "disabled",
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await dialog.getByLabel("User-Agent", { exact: true }).fill("");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const cleared = await (
    await page.request.get("/api/config/providers/ui-user-agent")
  ).json();
  expect(cleared.headers).toEqual({ "X-Title": "Preserved" });
  await page.route("**/api/control", async (route) => {
    if (route.request().postDataJSON().action !== "probe")
      return route.continue();
    await route.fulfill({
      status: 502,
      json: {
        error:
          "Model discovery for ui-user-agent failed (Hortator API HTTP 502): Provider returned HTTP 401: Unauthorized client",
        source: "provider",
        api_status: 502,
        upstream_status: 401,
      },
    });
  });
  const card = page.locator("article").filter({
    has: page.getByRole("heading", { name: "UI User Agent", exact: true }),
  });
  await card
    .getByRole("button", { name: "Discover models", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("Hortator API HTTP 502");
  await expect(page.getByRole("alert")).toContainText(
    "Provider returned HTTP 401",
  );
  await expect(page.getByRole("alert")).toContainText("Unauthorized client");
  await expect(page.getByLabel("Dashboard password")).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Providers", exact: true }),
  ).toBeVisible();
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

test("per-bot footer defaults, templates, validation and mobile preview persist", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Bots", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await page
    .getByRole("button", { name: "Edit Hortator", exact: true })
    .click();
  await dialog
    .getByRole("button", { name: "Message footer", exact: true })
    .click();
  const toggle = dialog.getByRole("checkbox", {
    name: "Show diagnostic footer",
  });
  await expect(toggle).toBeChecked();
  await expect(
    dialog.getByLabel("Footer template", { exact: true }),
  ).toHaveValue("TTFT: {{TTFT}} | TPS: {{TPS}}");
  await toggle.uncheck();
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(
    (await (await page.request.get("/api/config/bots/hortator")).json())
      .footer_enabled,
  ).toBe(false);

  const before = await (await page.request.get("/api/config/bots/ada")).json();
  await page.getByRole("button", { name: "Edit Ada", exact: true }).click();
  await dialog
    .getByRole("button", { name: "Message footer", exact: true })
    .click();
  await expect(toggle).not.toBeChecked();
  await toggle.check();
  const template =
    "{{BOT}} | {{MODEL SELECTED}} | {{PROVIDER}} | {{CONTEXT}} | {{TTFT}} | {{TPS}}";
  await dialog.getByLabel("Footer template", { exact: true }).fill(template);
  const preview = dialog.getByRole("region", {
    name: "Footer example preview",
  });
  await expect(preview).toContainText("1858ms | 477.7");
  await expect(preview).toContainText("Ada");
  await expect(preview).not.toContainText("{{");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(preview).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/footer-mobile.png",
    animations: "disabled",
  });
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const after = await (await page.request.get("/api/config/bots/ada")).json();
  expect(after.footer_enabled).toBe(true);
  expect(after.footer_template).toBe(template);
  for (const field of [
    "enabled",
    "model_profile_id",
    "persona",
    "enabled_plugins",
    "cooldown_seconds",
  ])
    expect(after[field]).toEqual(before[field]);

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("button", { name: "Edit Ada", exact: true }).click();
  await dialog
    .getByRole("button", { name: "Message footer", exact: true })
    .click();
  await expect(toggle).toBeChecked();
  await expect(
    dialog.getByLabel("Footer template", { exact: true }),
  ).toHaveValue(template);
  await dialog
    .getByText("Advanced · full configuration JSON", { exact: true })
    .click();
  const raw = dialog.getByLabel("Configuration record", { exact: true });
  expect(JSON.parse(await raw.inputValue()).footer_template).toBe(template);
  await dialog
    .getByLabel("Footer template", { exact: true })
    .fill("{{PASSWORD}}");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog.getByRole("alert")).toContainText(
    "Supported footer placeholders",
  );
  expect(
    (await (await page.request.get("/api/config/bots/ada")).json())
      .footer_template,
  ).toBe(template);
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
    .getByText(
      "Request body (credentials redacted; reasoning replay in private diagnostics)",
      {
        exact: true,
      },
    )
    .first()
    .click();
  await expect(
    inspector
      .locator("details")
      .filter({
        hasText:
          "Request body (credentials redacted; reasoning replay in private diagnostics)",
      })
      .first()
      .locator("pre"),
  ).toContainText("Browser verification fixture");
  const diagnostics = inspector
    .locator("details")
    .filter({ hasText: "Provider reasoning & diagnostics" })
    .first();
  await diagnostics.locator("summary").click();
  await expect(diagnostics.locator("pre")).toContainText(
    "hidden fixture reasoning",
  );
  await expect(diagnostics).toContainText(
    "Provider reasoning is captured below.",
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
