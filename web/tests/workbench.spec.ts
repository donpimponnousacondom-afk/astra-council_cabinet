import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import { dateLabel, setCouncilTimezone } from "../src/api";

// Playwright starts the existing /tmp/hortator-e2e-* fixture server. Saves below
// use that isolated API; density records and upstream errors are browser mocks.
// No test activates a bot, runs a model, or contacts a public provider.
const password = "test-only-password-never-use-in-production";
const navigate = async (page: Page, name: string) => {
  await page
    .getByRole("navigation", { name: "Workbench pages" })
    .getByRole("button", { name, exact: true })
    .click();
};
const record = async (page: Page, kind: string, id: string) => {
  const response = await page.request.get(`/api/config/${kind}/${id}`);
  expect(response.ok()).toBe(true);
  return response.json();
};

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Dashboard password").fill(password);
  await page.getByRole("button", { name: "Enter council control" }).click();
  await expect(
    page.getByRole("heading", { name: "The council", exact: true, level: 1 }),
  ).toBeVisible();
});

test("modern navigation keeps every operational page and compact source identities", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(
    page.getByRole("link", { name: "Legacy dashboard" }),
  ).toHaveAttribute("href", "/legacy/");
  const status = await (await page.request.get("/api/status")).json();
  setCouncilTimezone(status.settings.timezone);
  const version = status.version;
  const banner = page.getByRole("region", { name: "Running version" });
  await expect(banner).toContainText(version.short_commit);
  await expect(banner.locator("time")).toHaveText(
    dateLabel(version.committed_at),
  );
  await banner.getByText("Build details", { exact: true }).click();
  const serverCommit = banner.locator("dl > div").filter({
    has: page.getByText("Server commit", { exact: true }),
  });
  await expect(serverCommit.locator("dd")).toHaveText(version.commit);
  await expect(
    banner.getByText("Server source", { exact: true }),
  ).toBeVisible();
  await expect(
    banner.getByText("Dashboard source", { exact: true }),
  ).toBeVisible();
  await banner.getByText("Build details", { exact: true }).click();
  expect((await banner.boundingBox())!.height).toBeLessThanOrEqual(42);

  for (const name of [
    "Trajectory",
    "Analytics",
    "Bots",
    "Providers",
    "Model profiles",
    "Prompt library",
    "Plugins",
    "Rooms",
    "Council settings",
    "Discord commands",
    "Overview",
  ]) {
    await navigate(page, name);
    await expect(
      page.getByRole("heading", {
        name: name === "Overview" ? "The council" : name,
        exact: true,
        level: 1,
      }),
    ).toBeVisible();
  }
  await page.screenshot({
    path: testInfo.outputPath("modern-overview.png"),
    animations: "disabled",
  });
  expect(errors).toEqual([]);
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 1280, height: 800 },
]) {
  test(`six bots and providers fit without vertical scrolling at ${viewport.width}×${viewport.height}`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize(viewport);
    await page.route("**/api/status", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const bot = body.bots.find((item: { id: string }) => item.id === "ada");
      const provider = body.providers[0];
      body.bots = ["V", "Socrates", "Hortator", "Dirac", "Curie", "Ada"].map(
        (name, index) => ({
          ...bot,
          id: `wb-density-bot-${index}`,
          name,
          enabled: false,
          role: name === "Hortator" ? "hortator" : "council",
          active_turn: null,
          readiness: ["Synthetic disabled density fixture"],
          runtime: { ...bot.runtime, gateway_status: "offline", error: null },
        }),
      );
      body.providers = [
        "Test provider",
        "OpenRouter",
        "Ollama",
        "Featherless",
        "DeepSeek",
        "AGENTROUTER_CARLITOSLOPEZ",
      ].map((name, index) => ({
        ...provider,
        id: `wb-density-provider-${index}`,
        name,
        enabled: false,
        key_configured: true,
        base_url: `https://density-${index}.provider.invalid/v1`,
        health: {
          ...provider.health,
          circuit_until: 0,
          consecutive_failures: 0,
          last_error: null,
        },
      }));
      await route.fulfill({ json: body });
    });
    await page.getByRole("button", { name: "Refresh dashboard" }).click();

    for (const [name, label] of [
      ["Bots", "Bots"],
      ["Providers", "providers"],
    ]) {
      await navigate(page, name);
      const table = page.getByRole("table", { name: label, exact: true });
      const rows = table.locator("tbody tr");
      await expect(rows).toHaveCount(6);
      const bounds = await rows.evaluateAll((elements) =>
        elements.map((element) => {
          const { top, bottom, height } = element.getBoundingClientRect();
          return { top, bottom, height };
        }),
      );
      for (const row of bounds) {
        expect(row.top).toBeGreaterThan(0);
        expect(row.bottom).toBeLessThan(viewport.height - 24);
        expect(row.height).toBeLessThanOrEqual(64);
      }
      expect(
        await page.locator(".workbench-content").evaluate((el) => el.scrollTop),
      ).toBe(0);
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(viewport.width);
      await page.screenshot({
        path: testInfo.outputPath(`modern-${label}-${viewport.width}.png`),
        animations: "disabled",
      });
    }

    await navigate(page, "Bots");
    const table = page.getByRole("table", { name: "Bots", exact: true });
    const sort = table.getByRole("button", { name: "Bot / ID", exact: true });
    await sort.click();
    await expect(table.locator("thead th").first()).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
    await expect(table.locator("tbody tr").first()).toContainText("Ada");
    await sort.click();
    await expect(table.locator("thead th").first()).toHaveAttribute(
      "aria-sort",
      "descending",
    );
    await expect(
      table.locator("tbody tr").first().locator(".record-name strong"),
    ).toHaveText("V");
    await page.keyboard.press("/");
    const filter = page.getByRole("textbox", {
      name: "Search bots",
      exact: true,
    });
    await expect(filter).toBeFocused();
    await filter.fill("dirac");
    await expect(table.locator("tbody tr")).toHaveCount(1);
    await expect(table.locator("tbody tr")).toContainText("Dirac");
    await page.getByRole("button", { name: "Clear filter" }).click();
    await expect(table.locator("tbody tr")).toHaveCount(6);
  });
}

test("model table changes SSE immediately while preserving native vendor and compaction JSON", async ({
  page,
}) => {
  await navigate(page, "Model profiles");
  await page.getByRole("button", { name: "Add profile", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Display name", { exact: true })
    .fill("Workbench SSE fixture");
  await dialog
    .getByLabel("Stable identifier", { exact: true })
    .fill("wb-sse-fixture");
  await dialog
    .getByLabel("Exact model identifier", { exact: true })
    .fill("fixture/model");
  const parameters = {
    temperature: 0.41,
    thinking: { type: "enabled" },
    vendor: { preserve: ["one", 2, false] },
    stream_options: { include_usage: true },
  };
  await dialog
    .getByLabel("Model parameters", { exact: true })
    .fill(JSON.stringify(parameters));
  await dialog
    .getByText("Advanced · compaction parameter overrides", { exact: true })
    .click();
  const compaction = { reasoning_effort: "high", vendor: { compact: true } };
  await dialog
    .getByLabel("Compaction parameters", { exact: true })
    .fill(JSON.stringify(compaction));
  await dialog
    .getByRole("button", { name: "Create draft", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const before = await record(page, "profiles", "wb-sse-fixture");
  const toggle = page.getByRole("checkbox", {
    name: "SSE streaming for Workbench SSE fixture",
    exact: true,
  });
  await expect(toggle).toBeChecked();
  await toggle.uncheck();
  await expect
    .poll(async () => (await record(page, "profiles", "wb-sse-fixture")).stream)
    .toBe(false);
  const after = await record(page, "profiles", "wb-sse-fixture");
  expect(after.revision).toBe(before.revision + 1);
  expect(after.request_json).toEqual(parameters);
  expect(after.compaction_request_json).toEqual(compaction);
  expect(after.provider_id).toBe(before.provider_id);
  expect(after.include_usage).toBe(before.include_usage);
  await page
    .getByRole("button", { name: "Edit Workbench SSE fixture", exact: true })
    .click();
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
    .check();
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(toggle).toBeChecked();
  expect(
    (await record(page, "profiles", "wb-sse-fixture")).request_json,
  ).toEqual(parameters);
});

test("provider headers round-trip and a mocked upstream 401 through API 502 keeps owner login", async ({
  page,
}) => {
  await navigate(page, "Providers");
  await page.getByRole("button", { name: "Add provider", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Display name", { exact: true })
    .fill("Workbench provider fixture");
  await dialog
    .getByLabel("Stable identifier", { exact: true })
    .fill("wb-provider-fixture");
  await dialog
    .getByLabel("API base URL", { exact: true })
    .fill("https://fixture.provider.invalid/v1");
  await dialog
    .getByRole("checkbox", { name: "Provider enabled", exact: true })
    .uncheck();
  await dialog
    .getByText("Advanced · non-secret HTTP headers", { exact: true })
    .click();
  await dialog.getByLabel("Request headers", { exact: true }).fill(
    JSON.stringify({
      "uSeR-aGeNt": "Initial/1.0",
      "X-Title": "Preserved fixture",
    }),
  );
  await expect(dialog.getByLabel("User-Agent", { exact: true })).toHaveValue(
    "Initial/1.0",
  );
  await dialog
    .getByLabel("User-Agent", { exact: true })
    .fill("Workbench Client/2.0");
  await dialog
    .getByRole("button", { name: "Create draft", exact: true })
    .click();
  await expect(
    dialog.getByRole("heading", {
      name: "Workbench provider fixture",
      exact: true,
    }),
  ).toBeVisible();
  expect(
    (await record(page, "providers", "wb-provider-fixture")).headers,
  ).toEqual({
    "User-Agent": "Workbench Client/2.0",
    "X-Title": "Preserved fixture",
  });
  await dialog.getByLabel("User-Agent", { exact: true }).fill("");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(
    (await record(page, "providers", "wb-provider-fixture")).headers,
  ).toEqual({ "X-Title": "Preserved fixture" });
  let probes = 0;
  await page.route("**/api/control", async (route) => {
    if (route.request().postDataJSON().action !== "probe")
      return route.continue();
    probes += 1;
    await route.fulfill({
      status: 502,
      json: {
        error:
          "Model discovery failed (Hortator API HTTP 502): Provider returned HTTP 401: Unauthorized client [synthetic browser response]",
        source: "provider",
        api_status: 502,
        upstream_status: 401,
      },
    });
  });
  await page
    .getByRole("button", {
      name: "Discover models for Workbench provider fixture",
      exact: true,
    })
    .click();
  await expect(page.getByRole("alert")).toContainText("Hortator API HTTP 502");
  await expect(page.getByRole("alert")).toContainText(
    "Provider returned HTTP 401",
  );
  expect(probes).toBe(1);
  expect((await page.request.get("/api/auth/session")).status()).toBe(200);
  await expect(page.getByLabel("Dashboard password")).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Providers", exact: true, level: 1 }),
  ).toBeVisible();
});

test("bot silence and footer controls persist in a docked editor without changing other behavior", async ({
  page,
}, testInfo) => {
  await navigate(page, "Bots");
  await page.getByRole("button", { name: "Add bot", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Display name", { exact: true })
    .fill("Workbench bot fixture");
  await dialog
    .getByLabel("Stable identifier", { exact: true })
    .fill("wb-bot-fixture");
  await dialog
    .getByRole("button", { name: "Create draft", exact: true })
    .click();
  await expect(
    dialog.getByRole("heading", { name: "Workbench bot fixture", exact: true }),
  ).toBeVisible();
  const before = await record(page, "bots", "wb-bot-fixture");
  expect(before.enabled).toBe(false);
  await expect(dialog).toHaveAttribute("aria-modal", "false");
  await expect(
    page.getByRole("navigation", { name: "Workbench pages" }),
  ).toBeVisible();
  await dialog
    .getByRole("button", { name: "Capabilities", exact: true })
    .click();
  const silence = dialog.getByRole("checkbox", {
    name: "Allow intentional silence",
    exact: true,
  });
  await expect(silence).toBeChecked();
  expect((await silence.boundingBox())!.width).toBeGreaterThanOrEqual(16);
  await silence.uncheck();
  await dialog
    .getByRole("button", { name: "Message footer", exact: true })
    .click();
  await dialog
    .getByRole("checkbox", { name: "Show diagnostic footer", exact: true })
    .check();
  const template =
    "{{BOT}} | {{MODEL}} | {{PROVIDER}} | {{CONTEXT}} | {{TTFT}} | {{TPS}}";
  await dialog.getByLabel("Footer template", { exact: true }).fill(template);
  const preview = dialog.getByRole("region", {
    name: "Footer example preview",
  });
  await expect(preview).toContainText("1858ms | 477.7");
  await expect(preview).not.toContainText("{{");
  await page.screenshot({
    path: testInfo.outputPath("modern-footer-editor.png"),
    animations: "disabled",
  });
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const after = await record(page, "bots", "wb-bot-fixture");
  expect(after.allow_silence).toBe(false);
  expect(after.footer_enabled).toBe(true);
  expect(after.footer_template).toBe(template);
  for (const field of [
    "enabled",
    "model_profile_id",
    "persona",
    "enabled_plugins",
    "cooldown_seconds",
    "interval_seconds",
  ])
    expect(after[field]).toEqual(before[field]);
  await page
    .getByRole("button", { name: "Edit Workbench bot fixture", exact: true })
    .click();
  await dialog
    .getByRole("button", { name: "Capabilities", exact: true })
    .click();
  await expect(silence).not.toBeChecked();
  await dialog
    .getByRole("button", { name: "Message footer", exact: true })
    .click();
  await expect(
    dialog.getByLabel("Footer template", { exact: true }),
  ).toHaveValue(template);
  await dialog
    .getByRole("button", { name: "Close dialog", exact: true })
    .click();
});

test("docked editor protects unsaved changes and supports quick open, maximize and keyboard save", async ({
  page,
}, testInfo) => {
  await navigate(page, "Prompt library");
  await page.getByRole("button", { name: "Add prompt", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Display name", { exact: true })
    .fill("Workbench prompt fixture");
  await dialog
    .getByLabel("Stable identifier", { exact: true })
    .fill("wb-prompt-fixture");
  await dialog
    .getByLabel("System prompt", { exact: true })
    .fill("Synthetic workbench prompt.");
  await expect(
    dialog.getByText("Unsaved changes", { exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Control+s");
  await expect(dialog).toHaveCount(0);
  expect((await record(page, "prompts", "wb-prompt-fixture")).content).toBe(
    "Synthetic workbench prompt.",
  );

  await page.keyboard.press("Control+k");
  const palette = page.getByRole("dialog", { name: "Quick open", exact: true });
  await palette
    .getByRole("textbox", { name: "Go to page or record" })
    .fill("wb-prompt-fixture");
  await expect(palette.getByRole("option")).toHaveCount(1);
  await page.keyboard.press("Enter");
  await expect(palette).toHaveCount(0);
  await expect(
    dialog.getByRole("heading", {
      name: "Workbench prompt fixture",
      exact: true,
    }),
  ).toBeVisible();
  await dialog
    .getByLabel("System prompt", { exact: true })
    .fill("Unsaved version retained after cancelled navigation.");
  const discard = page.waitForEvent("dialog");
  const cancelledNavigation = navigate(page, "Providers");
  const confirmation = await discard;
  expect(confirmation.message()).toContain("Discard unsaved changes");
  await confirmation.dismiss();
  await cancelledNavigation;
  await expect(dialog.getByLabel("System prompt", { exact: true })).toHaveValue(
    "Unsaved version retained after cancelled navigation.",
  );
  await expect(
    page.getByRole("heading", {
      name: "Prompt library",
      exact: true,
      level: 1,
    }),
  ).toBeVisible();
  await dialog
    .getByRole("button", { name: "Maximize editor", exact: true })
    .click();
  await expect(page.locator(".dialog-dock")).toHaveClass(/is-maximized/);
  await dialog
    .getByRole("button", { name: "Restore editor size", exact: true })
    .click();
  await expect(page.locator(".dialog-dock")).not.toHaveClass(/is-maximized/);
  await page.screenshot({
    path: testInfo.outputPath("modern-docked-editor.png"),
    animations: "disabled",
  });
  await dialog.getByLabel("System prompt", { exact: true }).focus();
  await page.keyboard.press("Control+s");
  await expect(dialog).toHaveCount(0);
  expect((await record(page, "prompts", "wb-prompt-fixture")).content).toBe(
    "Unsaved version retained after cancelled navigation.",
  );

  await page
    .getByRole("button", { name: "Edit Workbench prompt fixture", exact: true })
    .click();
  await dialog
    .getByLabel("System prompt", { exact: true })
    .fill("Discard this change.");
  page.once("dialog", (confirmation) => confirmation.accept());
  await navigate(page, "Providers");
  await expect(dialog).toHaveCount(0);
  expect((await record(page, "prompts", "wb-prompt-fixture")).content).toBe(
    "Unsaved version retained after cancelled navigation.",
  );
  await page.keyboard.press("Control+b");
  await expect(page.locator(".workbench")).toHaveClass(/nav-collapsed/);
  await page.keyboard.press("Control+b");
  await expect(page.locator(".workbench")).not.toHaveClass(/nav-collapsed/);
});

test("seeded context and trajectory retain summary, notes, private reasoning, tools and delivery", async ({
  page,
}, testInfo) => {
  await navigate(page, "Bots");
  await page
    .getByRole("button", { name: "Inspect Ada context", exact: true })
    .click();
  const context = page.getByRole("dialog", {
    name: "Ada · context & memory",
    exact: true,
  });
  await expect(context.locator(".summary-text")).toContainText(
    "synthetic verification data",
  );
  await expect(
    context.locator(".memory-note").filter({ hasText: "fixture" }),
  ).toContainText("Inspect the timeline for observability.");
  await expect(
    context.getByRole("button", { name: "Edit note", exact: true }).first(),
  ).toBeVisible();
  await context
    .getByRole("button", { name: "Close dialog", exact: true })
    .click();
  await navigate(page, "Trajectory");
  await page.locator(".turn-row").filter({ hasText: "sent" }).first().click();
  const inspector = page.locator(".turn-inspector");
  await inspector
    .getByRole("button", { name: "requests", exact: true })
    .click();
  const request = inspector
    .locator("details")
    .filter({
      hasText:
        "Request body (credentials redacted; reasoning replay in private diagnostics)",
    })
    .first();
  await request.locator("summary").click();
  await expect(request.locator("pre")).toContainText(
    "Browser verification fixture",
  );
  const diagnostics = inspector
    .locator("details")
    .filter({ hasText: "Provider reasoning & diagnostics" })
    .first();
  await diagnostics.locator("summary").click();
  await expect(diagnostics.locator("pre")).toContainText(
    "hidden fixture reasoning",
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
  const download = page.waitForEvent("download");
  await inspector
    .getByRole("link", { name: "Export full trajectory", exact: true })
    .click();
  expect((await download).suggestedFilename()).toMatch(/^turn_.*\.json$/);
  await page.screenshot({
    path: testInfo.outputPath("modern-trajectory.png"),
    animations: "disabled",
  });
});

test("modern document and agent tool inspectors retain private draft downloads and paged evidence", async ({
  page,
}) => {
  const documents = await (await page.request.get("/api/documents")).json();
  const fixture = documents.sites.find(
    (site: { bot_id: string; site: string }) =>
      site.bot_id === "ada" && site.site === "sandbox-fixture",
  );
  expect(fixture).toMatchObject({
    title: "sandbox-fixture",
    revision: 8,
    published_revision: 7,
  });
  await navigate(page, "Plugins");
  await page
    .getByRole("button", { name: "Edit Documents & local sites", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("API key", { exact: true })).toHaveCount(0);
  const site = dialog.getByRole("article").filter({
    has: page.getByRole("heading", { name: "sandbox-fixture", exact: true }),
  });
  await expect(site).toBeVisible();
  await site.getByText("Download draft files (7)", { exact: true }).click();
  const draft = site
    .getByRole("link", { name: "index.html", exact: true })
    .first();
  const response = await page.request.get((await draft.getAttribute("href"))!);
  expect(response.headers()["content-disposition"]).toContain("attachment");
  expect(await response.text()).toContain("PRIVATE DRAFT");
  await dialog
    .getByRole("button", { name: "Close dialog", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Edit Private workspaces", exact: true })
    .click();
  await expect(dialog.getByLabel("API key", { exact: true })).toHaveCount(0);
  await dialog
    .getByRole("button", { name: "Inspect files", exact: true })
    .first()
    .click();
  await dialog.getByRole("button", { name: /notes.txt \(/ }).click();
  const evidence = dialog.locator(".agent-tool-inspection pre");
  await expect(evidence).toContainText("PRIVATE WORKSPACE NOTES");
  await dialog.getByRole("button", { name: "Next chunk", exact: true }).click();
  await expect(evidence).toContainText("Unicode café");
  await expect(evidence).not.toContainText("PRIVATE WORKSPACE NOTES");
  await dialog
    .getByRole("button", { name: "Inspect job", exact: true })
    .click();
  await dialog
    .getByRole("button", { name: "Read stderr", exact: true })
    .click();
  await expect(evidence).toContainText("Synthetic diagnostic line");
  await dialog
    .getByRole("button", { name: "Close dialog", exact: true })
    .click();
});

test("bot section switching preserves malformed plugin JSON until repaired or explicitly discarded", async ({
  page,
}) => {
  const before = await record(page, "bots", "ada");
  await navigate(page, "Bots");
  await page.getByRole("button", { name: "Edit Ada", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Ada", exact: true });
  await dialog
    .getByRole("button", { name: "Capabilities", exact: true })
    .click();
  await dialog
    .getByText("Advanced · per-bot plugin configuration", { exact: true })
    .click();
  const raw = dialog.getByLabel("Plugin overrides", { exact: true });
  const original = await raw.inputValue();
  const malformed = '{"document_site": {"local_base_url": ';
  await raw.fill(malformed);
  await dialog.getByRole("button", { name: "Identity", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText(
    "Fix the invalid JSON before changing sections",
  );
  await expect(
    dialog.getByRole("button", { name: "Capabilities", exact: true }),
  ).toHaveAttribute("aria-current", "page");
  await expect(raw).toHaveValue(malformed);
  await expect(raw).toBeFocused();
  await expect(
    dialog.getByText("Unsaved changes", { exact: true }),
  ).toBeVisible();
  await raw.fill(original);
  await dialog.getByRole("button", { name: "Identity", exact: true }).click();
  await expect(dialog.getByLabel("Display name", { exact: true })).toHaveValue(
    before.name,
  );
  await expect(dialog.getByRole("alert")).toHaveCount(0);
  await expect(
    dialog.getByText("No unsaved changes", { exact: true }),
  ).toBeVisible();

  await dialog
    .getByRole("button", { name: "Capabilities", exact: true })
    .click();
  await dialog
    .getByText("Advanced · per-bot plugin configuration", { exact: true })
    .click();
  await raw.fill(malformed);
  page.once("dialog", (confirmation) => confirmation.dismiss());
  await dialog
    .getByRole("button", { name: "Close dialog", exact: true })
    .click();
  await expect(raw).toHaveValue(malformed);
  page.once("dialog", (confirmation) => confirmation.accept());
  await dialog
    .getByRole("button", { name: "Close dialog", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(await record(page, "bots", "ada")).toEqual(before);
});

test("context note drafts are guarded across channel switches and global navigation", async ({
  page,
}) => {
  const firstChannel = "222222222222222222";
  const secondChannel = "777777777777777777";
  const originalContext = await (
    await page.request.get(`/api/context/ada/${firstChannel}`)
  ).json();
  // Only the rendered second channel is synthetic. No extra real context is created.
  await page.route("**/api/status", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const bot = body.bots.find((item: { id: string }) => item.id === "ada");
    bot.contexts = [
      {
        ...bot.contexts.find(
          (item: { channel_id: string }) => item.channel_id === firstChannel,
        ),
        channel_id: firstChannel,
      },
      { ...bot.contexts[0], channel_id: secondChannel },
    ];
    await route.fulfill({ json: body });
  });
  await page.route(`**/api/context/ada/${secondChannel}`, async (route) => {
    await route.fulfill({
      json: {
        ...originalContext,
        channel_id: secondChannel,
        summary: "Synthetic second channel for draft-navigation verification.",
        memories: [],
      },
    });
  });
  await page.getByRole("button", { name: "Refresh dashboard" }).click();
  await navigate(page, "Bots");
  await page
    .getByRole("button", { name: "Inspect Ada context", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Ada · context & memory",
    exact: true,
  });
  const channel = dialog.getByLabel("Channel / thread", { exact: true });
  const noteKey = dialog.getByLabel("Note key", { exact: true });
  const noteValue = dialog.getByLabel("Note value", { exact: true });
  await expect(channel).toHaveValue(firstChannel);
  await noteKey.fill("wb-unsaved-only");
  await noteValue.fill(
    "Keep this draft in its original channel until explicitly discarded.",
  );
  const change = page.waitForEvent("dialog");
  const switching = channel.selectOption(secondChannel);
  const confirmation = await change;
  expect(confirmation.message()).toContain("Discard the unsaved note changes");
  await confirmation.dismiss();
  await switching;
  await expect(channel).toHaveValue(firstChannel);
  await expect(noteKey).toHaveValue("wb-unsaved-only");
  await expect(noteValue).toHaveValue(
    "Keep this draft in its original channel until explicitly discarded.",
  );
  page.once("dialog", (confirmation) => confirmation.accept());
  await channel.selectOption(secondChannel);
  await expect(channel).toHaveValue(secondChannel);
  await expect(dialog.locator(".summary-text")).toContainText(
    "Synthetic second channel",
  );
  await expect(noteKey).toHaveValue("operator");
  await expect(noteValue).toHaveValue("");
  await noteValue.fill("Another unsaved draft.");
  page.once("dialog", (confirmation) => confirmation.dismiss());
  await navigate(page, "Providers");
  await expect(dialog).toBeVisible();
  await expect(noteValue).toHaveValue("Another unsaved draft.");
  page.once("dialog", (confirmation) => confirmation.accept());
  await navigate(page, "Providers");
  await expect(dialog).toHaveCount(0);
  const after = await (
    await page.request.get(`/api/context/ada/${firstChannel}`)
  ).json();
  expect(after.memories).toEqual(originalContext.memories);
});

test("quick open keeps keyboard focus inside the palette and follows arrow selection", async ({
  page,
}) => {
  const trigger = page.getByRole("button", {
    name: "Go to page or record Ctrl K",
    exact: true,
  });
  await trigger.click();
  const palette = page.getByRole("dialog", { name: "Quick open", exact: true });
  const input = palette.getByRole("textbox", {
    name: "Go to page or record",
    exact: true,
  });
  await expect(input).toBeFocused();
  const selected = palette.getByRole("option", { selected: true });
  await expect(selected).toContainText("Overview");
  await page.keyboard.press("ArrowDown");
  await expect(selected).toContainText("Trajectory");
  await page.keyboard.press("ArrowDown");
  await expect(selected).toContainText("Analytics");
  await page.keyboard.press("ArrowUp");
  await expect(selected).toContainText("Trajectory");
  for (const key of ["Tab", "Tab", "Shift+Tab"]) {
    await page.keyboard.press(key);
    await expect(input).toBeFocused();
  }
  await page.keyboard.press("Enter");
  await expect(palette).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Trajectory", level: 1, exact: true }),
  ).toBeVisible();
  await trigger.click();
  await input.fill("No matching record fixture 739281");
  await expect(palette.getByRole("option")).toHaveCount(0);
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(palette).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(palette).toHaveCount(0);
  await expect(trigger).toBeFocused();
});

test("an editor stays locked until the saved revision is acknowledged by a fresh dashboard response", async ({
  page,
}) => {
  const id = "wb-pending-save-fixture";
  const name = "Workbench pending save fixture";
  await navigate(page, "Prompt library");
  await page.getByRole("button", { name: "Add prompt", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Display name", { exact: true }).fill(name);
  await dialog.getByLabel("Stable identifier", { exact: true }).fill(id);
  await dialog
    .getByLabel("System prompt", { exact: true })
    .fill("Before pending acknowledgment.");
  await dialog
    .getByRole("button", { name: "Create draft", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await page.getByRole("button", { name: `Edit ${name}`, exact: true }).click();
  const savedText =
    "Saved on the server; keep this editor protected until the new revision is refreshed.";
  await dialog.getByLabel("System prompt", { exact: true }).fill(savedText);

  let releaseRefresh!: () => void;
  const refreshGate = new Promise<void>((resolve) => {
    releaseRefresh = resolve;
  });
  let markRefreshReached!: () => void;
  const refreshReached = new Promise<void>((resolve) => {
    markRefreshReached = resolve;
  });
  let mutationCommitted = false;
  let heldRefreshes = 0;
  await page.route("**/api/control", async (route) => {
    const body = route.request().postDataJSON();
    if (body.action !== "save" || body.kind !== "prompts" || body.id !== id)
      return route.continue();
    const response = await route.fetch();
    expect(response.ok()).toBe(true);
    mutationCommitted = true;
    await route.fulfill({ response });
  });
  await page.route("**/api/status", async (route) => {
    // Hold only requests that begin after the real save has completed. An older
    // overlapping poll cannot stand in for the post-mutation acknowledgment.
    const hold = mutationCommitted;
    const response = await route.fetch();
    if (hold) {
      heldRefreshes += 1;
      markRefreshReached();
      await refreshGate;
    }
    await route.fulfill({ response });
  });
  const confirmations: string[] = [];
  page.on("dialog", async (confirmation) => {
    confirmations.push(confirmation.message());
    await confirmation.dismiss();
  });
  try {
    await dialog
      .getByRole("button", { name: "Save changes", exact: true })
      .click();
    await refreshReached;
    expect((await record(page, "prompts", id)).content).toBe(savedText);
    await expect(
      dialog.getByRole("button", { name: "Saving…", exact: true }),
    ).toBeDisabled();
    await expect(
      dialog.getByLabel("Display name", { exact: true }),
    ).toBeDisabled();
    await expect(
      dialog.getByLabel("System prompt", { exact: true }),
    ).toBeDisabled();
    await navigate(page, "Providers");
    await expect(
      dialog.getByRole("heading", { name, exact: true }),
    ).toBeVisible();
    await expect(dialog.getByRole("alert")).toContainText(
      "Wait for the current save or action to finish",
    );
    await expect(
      page.getByRole("heading", {
        name: "Prompt library",
        exact: true,
        level: 1,
      }),
    ).toBeVisible();
    expect(confirmations).toEqual([]);
    expect(heldRefreshes).toBeGreaterThan(0);
  } finally {
    releaseRefresh();
  }
  await expect(dialog).toHaveCount(0);
  const row = page
    .getByRole("table", { name: "prompts", exact: true })
    .locator(`tr[data-record-id="${id}"]`);
  await expect(row).toContainText(savedText);
  await row.getByRole("button", { name: `Edit ${name}`, exact: true }).click();
  await expect(dialog.getByLabel("System prompt", { exact: true })).toHaveValue(
    savedText,
  );
  await expect(
    dialog.getByText("No unsaved changes", { exact: true }),
  ).toBeVisible();
  await dialog
    .getByRole("button", { name: "Close dialog", exact: true })
    .click();
});

test("malformed full-record JSON survives form edits until explicitly reset to current values", async ({
  page,
}) => {
  const id = "wb-raw-guard-fixture";
  await navigate(page, "Prompt library");
  await page.getByRole("button", { name: "Add prompt", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Display name", { exact: true })
    .fill("Workbench raw fixture");
  await dialog.getByLabel("Stable identifier", { exact: true }).fill(id);
  await dialog
    .getByLabel("System prompt", { exact: true })
    .fill("Preserve both the raw draft and accepted form values.");
  await dialog
    .getByText("Advanced · full configuration JSON", { exact: true })
    .click();
  const raw = dialog.getByLabel("Configuration record", { exact: true });
  const invalid = '{"name": "unfinished raw draft", ';
  await raw.fill(invalid);
  const correctedName = "Workbench corrected raw fixture";
  await dialog.getByLabel("Display name", { exact: true }).fill(correctedName);
  await expect(raw).toHaveValue(invalid);
  await expect(raw).toHaveClass(/invalid/);
  let createRequests = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/api/control") && request.method() === "POST") {
      const body = request.postDataJSON();
      if (body.action === "create" && body.id === id) createRequests += 1;
    }
  });
  await dialog
    .getByRole("button", { name: "Create draft", exact: true })
    .click();
  await expect(dialog).toBeVisible();
  await expect(raw).toHaveValue(invalid);
  expect(createRequests).toBe(0);
  await dialog
    .getByRole("button", { name: "Reset to current form values", exact: true })
    .click();
  expect(JSON.parse(await raw.inputValue())).toMatchObject({
    id,
    name: correctedName,
    content: "Preserve both the raw draft and accepted form values.",
  });
  await expect(raw).not.toHaveClass(/invalid/);
  await expect(
    dialog.getByRole("button", {
      name: "Reset to current form values",
      exact: true,
    }),
  ).toHaveCount(0);
  await dialog
    .getByRole("button", { name: "Create draft", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(createRequests).toBe(1);
  expect(await record(page, "prompts", id)).toMatchObject({
    name: correctedName,
    content: "Preserve both the raw draft and accepted form values.",
  });
});

test("per-bot credentials keep their selected destination and remain write-only through a pending save", async ({
  page,
}) => {
  const id = "wb-credential-fixture";
  const name = "Workbench credential fixture";
  const discardedValue = "synthetic-workbench-discarded-search-key";
  const savedValue = "synthetic-workbench-image-key";
  await navigate(page, "Bots");
  await page.getByRole("button", { name: "Add bot", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Display name", { exact: true }).fill(name);
  await dialog.getByLabel("Stable identifier", { exact: true }).fill(id);
  await dialog
    .getByRole("button", { name: "Create draft", exact: true })
    .click();
  await expect(
    dialog.getByRole("heading", { name, exact: true }),
  ).toBeVisible();
  await dialog
    .getByRole("button", { name: "Capabilities", exact: true })
    .click();
  const destination = dialog.getByLabel("Plugin credential", { exact: true });
  const entry = dialog.getByLabel("Per-bot plugin key", { exact: true });
  await expect(entry).toHaveAttribute("type", "password");
  await destination.selectOption("web_search");
  await entry.fill(discardedValue);
  page.once("dialog", (confirmation) => confirmation.dismiss());
  await destination.selectOption("image_generation");
  await expect(destination).toHaveValue("web_search");
  await expect(entry).toHaveValue(discardedValue);
  page.once("dialog", (confirmation) => confirmation.accept());
  await destination.selectOption("image_generation");
  await expect(destination).toHaveValue("image_generation");
  await expect(entry).toHaveValue("");
  await entry.fill(savedValue);

  let releaseCredential!: () => void;
  const credentialGate = new Promise<void>((resolve) => {
    releaseCredential = resolve;
  });
  let markStored!: () => void;
  const credentialStored = new Promise<void>((resolve) => {
    markStored = resolve;
  });
  const destinations: string[] = [];
  await page.route(`**/api/credentials/bots/${id}/*`, async (route) => {
    const field = decodeURIComponent(
      new URL(route.request().url()).pathname.split("/").at(-1)!,
    );
    destinations.push(field);
    expect(route.request().postDataJSON()).toEqual({ value: savedValue });
    const response = await route.fetch();
    expect(response.ok()).toBe(true);
    markStored();
    await credentialGate;
    await route.fulfill({ response });
  });
  try {
    await dialog
      .getByRole("button", { name: "Save credential", exact: true })
      .click();
    await credentialStored;
    await expect(
      dialog.getByRole("button", { name: "Verifying / saving…", exact: true }),
    ).toBeDisabled();
    await expect(entry).toBeDisabled();
    await expect(destination).toBeDisabled();
    await navigate(page, "Providers");
    await expect(
      dialog.getByRole("heading", { name, exact: true }),
    ).toBeVisible();
    await expect(dialog.getByRole("alert")).toContainText(
      "Wait for the current save or action to finish",
    );
    await expect(destination).toHaveValue("image_generation");
    const configured = await record(page, "bots", id);
    expect(configured.plugin_keys_configured).toContain("image_generation");
    expect(configured.plugin_keys_configured).not.toContain("web_search");
    expect(JSON.stringify(configured)).not.toContain(savedValue);
    expect(JSON.stringify(configured)).not.toContain(discardedValue);
  } finally {
    releaseCredential();
  }
  await expect(entry).toBeEnabled();
  await expect(entry).toHaveValue("");
  await expect(destination).toHaveValue("image_generation");
  expect(destinations).toEqual(["plugin:image_generation"]);
  const credentialBox = dialog.locator(".credential-box").filter({
    has: page.getByRole("heading", { name: "Per-bot plugin key", exact: true }),
  });
  await expect(
    credentialBox.getByText("Configured", { exact: true }),
  ).toBeVisible();
  await dialog
    .getByRole("button", { name: "Close dialog", exact: true })
    .click();
  await page.getByRole("button", { name: `Edit ${name}`, exact: true }).click();
  await dialog
    .getByRole("button", { name: "Capabilities", exact: true })
    .click();
  await destination.selectOption("image_generation");
  await expect(entry).toHaveValue("");
  await expect(
    credentialBox.getByText("Configured", { exact: true }),
  ).toBeVisible();
  expect((await record(page, "bots", id)).enabled).toBe(false);
  await dialog
    .getByRole("button", { name: "Close dialog", exact: true })
    .click();
});
