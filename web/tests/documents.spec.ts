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
    .getByLabel("Remote public base URL", { exact: true })
    .fill("https://council.example.test");
  await expect(
    dialog.getByRole("checkbox", {
      name: "Enable remote delivery",
      exact: true,
    }),
  ).not.toBeChecked();
  await expect(
    dialog.getByRole("checkbox", {
      name: "Automatically publish document changes",
      exact: true,
    }),
  ).not.toBeChecked();
  await dialog
    .getByLabel("SSH server", { exact: true })
    .fill("host.example.test");
  await expect(dialog.getByLabel("SSH port", { exact: true })).toHaveValue(
    "22",
  );
  await dialog.getByLabel("SSH username", { exact: true }).fill("publisher");
  await expect(
    dialog.getByLabel("SSH identity name", { exact: true }),
  ).toHaveValue("publishing");
  await dialog
    .getByLabel("Remote web directory", { exact: true })
    .fill("/home/publisher/public");
  await dialog
    .getByLabel("Remote history directory", { exact: true })
    .fill("/home/publisher/history");
  await dialog
    .getByLabel("Wait after latest change (seconds)", { exact: true })
    .fill("10");
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
  expect(plugin.config.remote).toMatchObject({
    host: "host.example.test",
    username: "publisher",
    web_root: "/home/publisher/public",
    state_root: "/home/publisher/history",
    debounce_seconds: 10,
  });
  expect(plugin.config.remote_enabled).not.toBe(true);
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
    .getByLabel("Bot local publication base URL", { exact: true })
    .fill("http://127.0.0.1:8000");
  await expect(dialog.getByLabel("SSH server", { exact: true })).toHaveCount(0);
  await expect(
    dialog.getByRole("checkbox", {
      name: "Enable remote delivery",
      exact: true,
    }),
  ).toHaveCount(0);
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const bot = await (await page.request.get("/api/config/bots/ada")).json();
  expect(bot.document_task_rounds).toBe(25);
  expect(bot.enabled_plugins).toContain("document_site");
  expect(bot.plugin_config.document_site).toEqual({
    local_base_url: "http://127.0.0.1:8000",
  });
});

test("publication status distinguishes queued work from confirmed remote revisions", async ({
  page,
}) => {
  const base = {
    site: "weekly-report",
    bot_id: "ada",
    title: "Weekly report",
    revision: 1,
    published_revision: 1,
    local_ready: true,
    local_path: "/sites/ada/weekly-report/",
    planned_public_url: "https://council.example.test/ada/weekly-report/",
    public_url: null as string | null,
    synced_revision: 0,
    delivery_current: false,
    remote_status: "queued",
    files: [],
    sync: {
      id: "sync-synthetic-browser-fixture",
      revision: 1,
      status: "queued",
      attempts: 0,
      last_error: null as string | null,
      retry_at: null as number | null,
      delivered_at: null as number | null,
      snapshot_commit: "1234567890abcdef1234567890abcdef12345678",
      remote_commit: null as string | null,
    },
  };
  let site = structuredClone(base);
  await page.route("**/api/documents", async (route) => {
    await route.fulfill({
      json: {
        sites: [site],
        publishing: { enabled: true, configured: true, status: "ready" },
      },
    });
  });
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
  const card = dialog.getByRole("article").filter({
    has: page.getByRole("heading", { name: "Weekly report", exact: true }),
  });
  const remoteLink = card.getByRole("link", {
    name: "Open remote published site",
    exact: true,
  });
  await expect(card).toContainText("Remote · Queued");
  await expect(card).toContainText(
    "Planned remote URL (delivery not confirmed)",
  );
  await expect(remoteLink).toHaveCount(0);
  await expect(dialog).toContainText(
    "Bots must create a named site before editing it.",
  );
  await card
    .getByText("Delivery details · revision 1", { exact: true })
    .click();
  await expect(card).toContainText(
    `Local snapshot commit: ${base.sync.snapshot_commit}`,
  );
  await expect(card).toContainText("Queue ID: sync-synthetic-browser-fixture");

  site = {
    ...site,
    remote_status: "syncing",
    sync: { ...site.sync, status: "syncing", attempts: 1 },
  };
  await dialog
    .getByRole("button", { name: "Refresh sites", exact: true })
    .click();
  await expect(card).toContainText("Remote · Uploading");
  await expect(remoteLink).toHaveCount(0);

  site = {
    ...site,
    remote_status: "delivered",
    public_url: base.planned_public_url,
    synced_revision: 1,
    delivery_current: true,
    sync: {
      ...site.sync,
      status: "delivered",
      delivered_at: 1788829200,
      remote_commit: "abcdef1234567890abcdef1234567890abcdef12",
    },
  };
  await dialog
    .getByRole("button", { name: "Refresh sites", exact: true })
    .click();
  await expect(card).toContainText("Includes all current edits");
  await expect(remoteLink).toHaveAttribute("href", base.planned_public_url);
  await expect(remoteLink).toHaveAttribute("rel", "noopener noreferrer");
  await expect(card).toContainText(
    "Remote history commit: abcdef1234567890abcdef1234567890abcdef12",
  );
  await expect(card).not.toContainText(
    "Planned remote URL (delivery not confirmed)",
  );

  site = {
    ...site,
    revision: 2,
    published_revision: 2,
    remote_status: "queued",
    delivery_current: false,
    sync: { ...base.sync, revision: 2, status: "queued" },
  };
  await dialog
    .getByRole("button", { name: "Refresh sites", exact: true })
    .click();
  await expect(card).toContainText("Last delivered revision 1");
  await expect(card).toContainText("Newer edits are not confirmed remotely");
  await expect(remoteLink).toHaveAttribute("href", base.planned_public_url);

  site = {
    ...site,
    remote_status: "failed",
    sync: {
      ...site.sync,
      status: "failed",
      attempts: 3,
      last_error: "Synthetic transport failure; no remote connection attempted",
      retry_at: 1788829800,
    },
  };
  await dialog
    .getByRole("button", { name: "Refresh sites", exact: true })
    .click();
  await expect(card).toContainText("Remote · Failed");
  await expect(card.getByRole("alert")).toHaveText(
    "Last delivery error: Synthetic transport failure; no remote connection attempted",
  );
  await expect(card).toContainText("Attempts: 3");
  await expect(card).toContainText("Retry at:");
  await expect(remoteLink).toHaveAttribute("href", base.planned_public_url);

  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await dialog.evaluate(
      (element) => element.scrollWidth <= element.clientWidth + 1,
    ),
  ).toBe(true);
  await card
    .getByRole("heading", { name: "Weekly report", exact: true })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: "test-results/document-publishing-mobile.png",
    fullPage: false,
  });
});
