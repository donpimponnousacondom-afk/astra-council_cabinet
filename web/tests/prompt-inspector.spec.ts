import { expect, test } from "@playwright/test";

test("prompt inspection follows assembly order, explains gates and preserves drafts", async ({
  page,
}, testInfo) => {
  await page.goto("/");
  await page
    .getByLabel("Dashboard password")
    .fill("test-only-password-never-use-in-production");
  await page.getByRole("button", { name: "Enter council control" }).click();
  await expect(
    page.getByRole("heading", { name: "The council", exact: true }),
  ).toBeVisible();
  const csrf = (await (await page.request.get("/api/auth/session")).json())
    .csrf;
  const control = async (
    action: string,
    kind: string,
    id: string,
    data: object,
  ) => {
    const response = await page.request.post("/api/control", {
      headers: { "X-CSRF-Token": csrf },
      data: { action, kind, id, data },
    });
    expect(response.ok(), await response.text()).toBe(true);
  };
  for (const [id, name, content, runtime_layer] of [
    [
      "inspector-persona",
      "Custom personality wrapper",
      "Draft personality follows: {persona} {unknown} {toString}",
      "persona",
    ],
    ["inspector-empty", "Blank tail", "", "dynamic_prompt"],
    ["inspector-z", "Z shared", "First shared prompt", null],
    ["inspector-a", "A shared", "Second shared prompt", null],
  ])
    await control("create", "prompts", id!, { name, content, runtime_layer });
  await control("clone", "bots", "ada", {
    id: "prompt-inspector-fixture",
    name: "Prompt inspector fixture",
  });
  await control("save", "bots", "prompt-inspector-fixture", {
    enabled_plugins: ["memory"],
    prompt_ids: ["inspector-z", "inspector-a"],
    disabled_prompt_layers: [],
    persona: "Saved personality",
    dynamic_prompt: "Tail at {now}",
    prompt_layer_overrides: { persona: "inspector-persona" },
    allow_images: true,
    allow_silence: true,
  });
  await page.reload();
  await page
    .getByRole("navigation", { name: "Workbench pages" })
    .getByRole("button", { name: "Bots", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Edit Prompt inspector fixture", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "Prompts", exact: true }).click();
  const ids = await dialog
    .locator("[data-layer]")
    .evaluateAll((rows) => rows.map((row) => row.getAttribute("data-layer")));
  expect(ids.indexOf("persona")).toBeLessThan(ids.indexOf("transcript"));
  expect(ids.indexOf("transcript")).toBeLessThan(ids.indexOf("dynamic_prompt"));
  expect(ids.indexOf("dynamic_prompt")).toBeLessThan(
    ids.indexOf("compaction_instructions"),
  );
  expect(
    await dialog.locator(".prompt-shared-insertion summary").allTextContents(),
  ).toEqual(["1. Z shared · system", "2. A shared · system"]);
  const mutations: string[] = [];
  page.on("request", (request) => {
    if (request.method() !== "GET") mutations.push(request.url());
  });
  const row = (id: string) => dialog.locator(`[data-layer="${id}"]`);
  await expect(row("director")).toContainText("Not applicable");
  await expect(row("director").getByRole("checkbox")).toBeChecked();
  await row("director").getByRole("button").focus();
  await page.keyboard.press("Enter");
  await expect(
    dialog.getByRole("region", {
      name: "Hortator director guidance inspection",
    }),
  ).toContainText("Director guidance is skipped");
  await expect(row("scheduled_alarm")).toContainText("Not applicable");
  await row("scheduled_alarm").getByRole("button").click();
  await expect(
    dialog.getByRole("region", { name: "Secretary alarm wake-up inspection" }),
  ).toContainText("secretary is not granted");
  await expect(row("background_completion")).toContainText("Turn only");
  await row("memory_budget").getByRole("button").click();
  await expect(
    dialog.getByText("Template text · runtime-memory-budget", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByText("Template text · runtime-memory-budget-over", {
      exact: true,
    }),
  ).toBeVisible();
  await dialog
    .getByLabel("Personality / system instructions", { exact: true })
    .fill("Unsaved personality {now}");
  await row("persona").getByRole("button").click();
  const inspect = dialog.getByRole("region", {
    name: "Bot personality inspection",
  });
  await expect(inspect).toContainText("Bot override");
  await expect(inspect).toContainText(
    "Draft personality follows: {persona} {unknown} {toString}",
  );
  await expect(inspect).toContainText("Unsaved personality {now}");
  await expect(inspect).toContainText("unknown names remain literal");
  await page
    .getByRole("button", { name: "Refresh dashboard", exact: true })
    .click();
  await expect(inspect).toBeVisible();
  await expect(inspect).toContainText("Unsaved personality {now}");
  expect(mutations).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("prompt-inspector.png") });
  await expect(
    dialog.getByRole("button", { name: "Save changes", exact: true }),
  ).toBeEnabled();
  await dialog.getByLabel("Include Bot personality", { exact: true }).uncheck();
  await expect(row("persona")).toContainText("Disabled");
  await dialog
    .getByLabel("Template for Bot dynamic prompt tail", { exact: true })
    .selectOption("inspector-empty");
  await expect(row("dynamic_prompt")).toContainText("Empty");
  await dialog
    .getByRole("button", { name: "Capabilities", exact: true })
    .click();
  await dialog.getByLabel("Receive image inputs", { exact: true }).uncheck();
  await dialog
    .getByLabel("Allow intentional silence", { exact: true })
    .uncheck();
  await dialog.getByRole("button", { name: "Prompts", exact: true }).click();
  await expect(row("image_disabled")).toContainText("Eligible");
  await row("silence_policy").getByRole("button").click();
  await expect(
    dialog.getByText("Template text · runtime-silence-policy-disabled", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    dialog.getByText("Template text · runtime-silence-policy", { exact: true }),
  ).toHaveCount(0);
  await expect(
    dialog.getByLabel("Personality / system instructions", { exact: true }),
  ).toHaveValue("Unsaved personality {now}");
  // A global plugin switch can change during polling while this draft stays open.
  await page.route("**/api/status", async (route) => {
    const response = await route.fetch();
    const status = await response.json();
    status.plugins.find((p: { id: string }) => p.id === "memory").enabled =
      false;
    await route.fulfill({ json: status });
  });
  await page
    .getByRole("button", { name: "Refresh dashboard", exact: true })
    .click();
  await expect(row("memory_budget")).toContainText("Not applicable");
  await row("memory_budget").getByRole("button").click();
  await expect(
    dialog.getByRole("region", { name: "Channel memory budget inspection" }),
  ).toContainText("memory is switched off globally");
  await expect(row("memory_budget").getByRole("checkbox")).toBeChecked();
  page.once("dialog", (d) => d.accept());
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  const saved = await (
    await page.request.get("/api/config/bots/prompt-inspector-fixture")
  ).json();
  expect(saved.persona).toBe("Saved personality");
  expect(saved.disabled_prompt_layers).toEqual([]);
});
