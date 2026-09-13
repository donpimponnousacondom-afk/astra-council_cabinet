import { expect, test } from "@playwright/test";

// Isolated fixture API. Granting the ingress plugin to a disabled fixture bot
// does not connect an application, register commands remotely or invoke a model.
const password = "test-only-password-never-use-in-production";

test("slash setup is opt-in per bot and shows installation guidance without changing ordinary chat", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByLabel("Dashboard password").fill(password);
  await page.getByRole("button", { name: "Enter council control" }).click();
  await expect(
    page.getByRole("heading", { name: "The council", exact: true, level: 1 }),
  ).toBeVisible();
  const session = await (await page.request.get("/api/auth/session")).json();
  const id = "wb-loki-slash";
  const result = await page.request.post("/api/control", {
    headers: { "X-CSRF-Token": session.csrf },
    data: {
      action: "create",
      kind: "bots",
      id,
      data: {
        id,
        name: id,
        model_profile_id: "balanced",
        application_id: "999999999999999991",
        enabled: false,
        persona: "Existing chaos companion personality",
        interval_seconds: 120,
        cooldown_seconds: 60,
      },
    },
  });
  expect(result.ok()).toBe(true);
  const original = await result.json();
  await page
    .getByRole("button", { name: "Refresh dashboard", exact: true })
    .click();
  await page
    .getByRole("navigation", { name: "Workbench pages" })
    .getByRole("button", { name: "Bots", exact: true })
    .click();
  await page.getByRole("button", { name: `Edit ${id}`, exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("button", { name: "Capabilities", exact: true })
    .click();
  await expect(
    dialog.getByText("Slash command setup", { exact: true }),
  ).toHaveCount(0);
  const checkbox = dialog.getByRole("checkbox", {
    name: /Slash command assistant/,
  });
  await expect(checkbox).not.toBeChecked();
  await checkbox.check();
  const setup = dialog.getByRole("group", {
    name: "Slash command setup",
    exact: true,
  });
  await expect(setup).toBeVisible();
  await expect(setup).toContainText("User Install");
  await expect(setup).toContainText("Guild Install");
  await expect(setup).toContainText("surrounding channel history is not read");
  await expect(setup).toContainText("14 minutes");
  await expect(
    setup.getByRole("link", {
      name: "Install this application's commands to your Discord account",
    }),
  ).toHaveAttribute(
    "href",
    "https://discord.com/oauth2/authorize?client_id=999999999999999991&scope=applications.commands&integration_type=1",
  );
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const saved = await (await page.request.get(`/api/config/bots/${id}`)).json();
  expect(saved.enabled_plugins).toEqual(["slash_commands"]);
  for (const field of [
    "enabled",
    "persona",
    "room_ids",
    "model_profile_id",
    "interval_seconds",
    "cooldown_seconds",
  ])
    expect(saved[field]).toEqual(original[field]);
  expect(saved.slash_commands.enabled).toBe(false);
  expect(saved.slash_commands.registered).toBe(false);
  const hortator = await (
    await page.request.get("/api/config/bots/hortator")
  ).json();
  expect(hortator.enabled_plugins).not.toContain("slash_commands");
  // Server registration status is a mocked read: no Discord API is contacted.
  await page.route("**/api/status", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    const bot = data.bots.find((value: { id: string }) => value.id === id);
    if (bot)
      bot.slash_commands = {
        ...saved.slash_commands,
        enabled: true,
        global_enabled: true,
        registered: true,
        command_id: "123456789012345678",
      };
    await route.fulfill({ response, json: data });
  });
  await page
    .getByRole("button", { name: "Refresh dashboard", exact: true })
    .click();
  await page.getByRole("button", { name: `Edit ${id}`, exact: true }).click();
  await dialog
    .getByRole("button", { name: "Capabilities", exact: true })
    .click();
  await expect(
    dialog.getByText("/prompt registered", { exact: true }),
  ).toBeVisible();
  await checkbox.uncheck();
  await expect(
    dialog.getByText("Slash command setup", { exact: true }),
  ).toHaveCount(0);
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  const disabled = await (
    await page.request.get(`/api/config/bots/${id}`)
  ).json();
  expect(disabled.enabled_plugins).toEqual([]);
});
