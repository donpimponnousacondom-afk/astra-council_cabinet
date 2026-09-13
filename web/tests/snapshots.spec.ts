import { expect, test } from "@playwright/test";
const id = "20260913T110000000000Z-123456abcdef";
const entry = {
  id,
  name: "Before experiment",
  created_at: "2026-09-13T11:00:00Z",
  source_commit: "0123456789abcdef",
  format_version: 1,
  bytes: 1048576,
  file_count: 8,
  compatible: true,
  reasons: [],
  bots: [{ id: "ada", channels: ["123456789012345678"] }],
};
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
test("named capture and confirmed scoped notes restore stay separate from runtime resume", async ({
  page,
}) => {
  const mutations: any[] = [];
  let paused = false;
  await page.route("**/api/snapshots**", async (route) => {
    const req = route.request();
    if (req.method() === "POST") {
      mutations.push({
        path: new URL(req.url()).pathname,
        body: req.postDataJSON(),
      });
      if (req.url().endsWith("/restore")) paused = true;
      if (req.url().endsWith("/resume")) paused = false;
      await route.fulfill({ json: { snapshot: entry, paused } });
    } else
      await route.fulfill({
        json: {
          snapshots: [entry],
          directory: "/isolated/snapshots",
          paused,
          busy: false,
        },
      });
  });
  await page
    .getByRole("navigation", { name: "Workbench pages" })
    .getByRole("button", { name: "Snapshots", exact: true })
    .click();
  await page
    .getByLabel("Snapshot name", { exact: true })
    .fill("Before unstable model");
  await page
    .getByLabel("Snapshot note", { exact: true })
    .fill("Private experiment");
  await page.getByRole("button", { name: "Capture snapshot" }).click();
  await expect(page.getByLabel("Snapshot name", { exact: true })).toHaveValue(
    "",
  );
  expect(mutations[0].body).toEqual({
    name: "Before unstable model",
    note: "Private experiment",
  });
  await page.getByRole("button", { name: `Before experiment ${id}` }).click();
  await page.getByLabel("Restore bot", { exact: true }).selectOption("ada");
  await expect(
    page.getByRole("button", { name: "Restore snapshot and pause" }),
  ).toBeDisabled();
  await page.getByLabel("Confirm snapshot ID", { exact: true }).fill(id);
  await page
    .getByRole("button", { name: "Restore snapshot and pause" })
    .click();
  await expect(
    page.getByText("Runtime paused after restore", { exact: true }),
  ).toBeVisible();
  expect(mutations[1]).toEqual({
    path: `/api/snapshots/${id}/restore`,
    body: {
      scope: "bot",
      confirmation: id,
      bot_id: "ada",
      include_context: false,
      include_global_memory: false,
    },
  });
  await page
    .getByRole("button", { name: "Resume runtime", exact: true })
    .click();
  await expect(
    page.getByText("Runtime paused after restore", { exact: true }),
  ).toHaveCount(0);
  expect(mutations[2].path).toBe("/api/snapshots/resume");
});
test("incompatible restore stays blocked and cancelled navigation preserves the draft", async ({
  page,
}) => {
  await page.route("**/api/snapshots", (route) =>
    route.fulfill({
      json: {
        snapshots: [
          {
            ...entry,
            compatible: false,
            reasons: ["Snapshot requires its exact source commit"],
          },
        ],
        directory: "/isolated/snapshots",
        paused: false,
        busy: false,
      },
    }),
  );
  await page
    .getByRole("navigation", { name: "Workbench pages" })
    .getByRole("button", { name: "Snapshots", exact: true })
    .click();
  await page.getByRole("button", { name: `Before experiment ${id}` }).click();
  await expect(
    page.getByText(
      /Restore blocked: Snapshot requires its exact source commit/,
    ),
  ).toBeVisible();
  await page.getByLabel("Restore scope", { exact: true }).selectOption("full");
  await page.getByLabel("Confirm snapshot ID", { exact: true }).fill(id);
  await expect(
    page.getByRole("button", { name: "Restore snapshot and pause" }),
  ).toBeDisabled();
  page.once("dialog", (dialog) => dialog.dismiss());
  await page
    .getByRole("navigation", { name: "Workbench pages" })
    .getByRole("button", { name: "Overview", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Snapshots", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByLabel("Confirm snapshot ID", { exact: true }),
  ).toHaveValue(id);
});
