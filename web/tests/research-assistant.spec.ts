import { expect, test } from "@playwright/test";

test("research configuration saves independently and bot Control inspects/cancels saved jobs", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByLabel("Dashboard password")
    .fill("test-only-password-never-use-in-production");
  await page.getByRole("button", { name: "Enter council control" }).click();
  await expect(
    page.getByRole("heading", { name: "The council", exact: true }),
  ).toBeVisible();
  const before = await (await page.request.get("/api/config/bots/ada")).json();
  const navigate = async (name: string) =>
    page
      .getByRole("navigation", { name: "Workbench pages" })
      .getByRole("button", { name, exact: true })
      .click();
  await navigate("Plugins");
  await page
    .getByRole("button", {
      name: "Edit Sub-agent researcher · experimental",
      exact: true,
    })
    .click();
  let dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Research model profile", { exact: true })
    .selectOption("balanced");
  await dialog
    .getByLabel("Research output ceiling (tokens)", { exact: true })
    .fill("4096");
  await dialog
    .getByLabel("Total research deadline (seconds)", { exact: true })
    .fill("900");
  await dialog
    .getByLabel("Researcher system prompt", { exact: true })
    .fill("Research carefully. Today is {now}.");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const plugin = await (
    await page.request.get("/api/config/plugins/research_assistant")
  ).json();
  expect(plugin.enabled).toBe(false);
  expect(plugin.config).toMatchObject({
    profile_id: "balanced",
    max_output_tokens: 4096,
    timeout_seconds: 900,
  });
  const after = await (await page.request.get("/api/config/bots/ada")).json();
  expect(after).toEqual(before);

  // UI inspection uses synthetic job responses. No worker/model/Discord call starts.
  let cancelled = false;
  await page.route("**/api/background-jobs?*", async (route) =>
    route.fulfill({
      json: [
        {
          id: "bg_fixture",
          bot_id: "ada",
          plugin: "research_assistant",
          state: "completed",
          notification: cancelled ? "revoked" : "pending",
          created_at: 1789999999,
        },
      ],
    }),
  );
  await page.route("**/api/background-jobs/bg_fixture?*", async (route) =>
    route.fulfill({
      json: {
        state: "completed",
        elapsed_seconds: 3,
        assignment: "Find official sources",
        metrics: { tps: 42 },
        content: "Saved report with sources",
        total_chars: 25,
        next_offset: null,
      },
    }),
  );
  await page.route(
    "**/api/background-jobs/bg_fixture/cancel",
    async (route) => {
      expect(route.request().method()).toBe("POST");
      cancelled = true;
      await route.fulfill({ json: { notification: "revoked" } });
    },
  );
  await navigate("Bots");
  await page.getByRole("button", { name: "Edit Ada", exact: true }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "Control", exact: true }).click();
  await dialog.getByRole("button", { name: "bg_fixture", exact: true }).click();
  await dialog.getByText("Saved result page", { exact: true }).click();
  await expect(
    dialog.getByText("Saved report with sources", { exact: true }),
  ).toBeVisible();
  await dialog
    .getByRole("button", { name: "Cancel job / follow-up", exact: true })
    .click();
  await expect(
    dialog.getByRole("table", { name: "Background jobs" }),
  ).toContainText("revoked");
  expect(cancelled).toBe(true);
});
