import { expect, test, type Page } from "@playwright/test";

// Isolated fixture API only; no bot is activated and no provider is contacted.
const password = "test-only-password-never-use-in-production";
async function navigate(page: Page, name: string) {
  await page
    .getByRole("navigation", { name: "Workbench pages" })
    .getByRole("button", { name, exact: true })
    .click();
}
async function addBot(page: Page, id: string) {
  const session = await (await page.request.get("/api/auth/session")).json();
  const result = await page.request.post("/api/control", {
    headers: { "X-CSRF-Token": session.csrf },
    data: {
      action: "create",
      kind: "bots",
      id,
      data: { id, name: id, model_profile_id: "balanced", enabled: false },
    },
  });
  expect(result.ok()).toBe(true);
  expect((await result.json()).contexts).toEqual([]);
  await page
    .getByRole("button", { name: "Refresh dashboard", exact: true })
    .click();
  await navigate(page, "Bots");
  await page.getByRole("button", { name: `Edit ${id}`, exact: true }).click();
}

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Dashboard password").fill(password);
  await page.getByRole("button", { name: "Enter council control" }).click();
  await expect(
    page.getByRole("heading", { name: "The council", level: 1, exact: true }),
  ).toBeVisible();
});

test("global notes work without channel contexts; quota stays per bot and notes can be edited while disabled", async ({
  page,
}, testInfo) => {
  const id = "wb-global-memory";
  await addBot(page, id);
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("button", { name: "Capabilities", exact: true })
    .click();
  const budget = dialog.getByLabel(
    "Global memory budget (characters across channels)",
    { exact: true },
  );
  await expect(budget).toHaveValue("48000");
  await expect(budget).toHaveAttribute("min", "1");
  await expect(budget).toHaveAttribute("max", "48000");
  for (const invalid of ["0", "-1", "48001", ""]) {
    await budget.fill(invalid);
    expect(
      await budget.evaluate((input: HTMLInputElement) => input.checkValidity()),
    ).toBe(false);
  }
  await budget.fill("100");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const saved = await (await page.request.get(`/api/config/bots/${id}`)).json();
  expect(saved.global_memory_char_limit).toBe(100);
  expect(saved.memory_char_limit).toBe(48000);
  expect(saved.enabled_plugins).toEqual([]);
  expect(
    (await (await page.request.get("/api/config/bots/hortator")).json())
      .global_memory_char_limit,
  ).toBe(48000);
  await page.getByRole("button", { name: `Edit ${id}`, exact: true }).click();
  await dialog
    .getByRole("button", { name: "Global notes", exact: true })
    .click();
  await expect(
    dialog.getByText("No global notes yet", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByText("Plugin disabled · notes retained", { exact: true }),
  ).toBeVisible();
  await dialog
    .getByLabel("Global note key", { exact: true })
    .fill("owner-preference");
  await dialog
    .getByLabel("Global note content", { exact: true })
    .fill("x".repeat(104));
  await dialog
    .getByRole("button", { name: "Save global note", exact: true })
    .click();
  await expect(
    dialog.getByRole("table", { name: "Global memory notes" }),
  ).toContainText("owner-preference");
  await expect(dialog.getByRole("status")).toContainText(
    "4 characters over budget",
  );
  await dialog
    .getByLabel("Global note content", { exact: true })
    .fill("Keep replies concise; preference stated by the owner.");
  await dialog
    .getByRole("button", { name: "Save global note", exact: true })
    .click();
  await expect(dialog.getByRole("status")).toHaveText("Global note saved.");
  const view = await (
    await page.request.get(`/api/global-memory/${id}`)
  ).json();
  expect(view.notes).toHaveLength(1);
  expect(view.notes[0].source_channel_id).toBeNull();
  expect(view.budget.must_consolidate).toBe(false);
  await page.screenshot({
    path: testInfo.outputPath("global-memory-editor.png"),
    animations: "disabled",
  });
  page.once("dialog", (confirmation) => confirmation.accept());
  await dialog
    .getByRole("button", { name: "Delete global note", exact: true })
    .click();
  await expect(
    dialog.getByText("No global notes yet", { exact: true }),
  ).toBeVisible();
});

test("global note drafts survive section switches and block discarded navigation and configuration saves", async ({
  page,
}) => {
  await addBot(page, "wb-global-draft");
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("button", { name: "Global notes", exact: true })
    .click();
  await dialog.getByLabel("Global note key", { exact: true }).fill("draft");
  await dialog
    .getByLabel("Global note content", { exact: true })
    .fill("A draft which must stay here.");
  await dialog
    .getByRole("button", { name: "Capabilities", exact: true })
    .click();
  await dialog
    .getByRole("button", { name: "Global notes", exact: true })
    .click();
  await expect(
    dialog.getByLabel("Global note content", { exact: true }),
  ).toHaveValue("A draft which must stay here.");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog.getByRole("alert")).toContainText(
    "Save or discard the pending global note first",
  );
  page.once("dialog", (confirmation) => confirmation.dismiss());
  await navigate(page, "Providers");
  await expect(
    dialog.getByLabel("Global note content", { exact: true }),
  ).toHaveValue("A draft which must stay here.");
  page.once("dialog", (confirmation) => confirmation.dismiss());
  await dialog
    .getByRole("button", { name: "New global note", exact: true })
    .click();
  await expect(
    dialog.getByLabel("Global note content", { exact: true }),
  ).toHaveValue("A draft which must stay here.");
  await dialog
    .getByRole("button", { name: "Save global note", exact: true })
    .click();
  await expect(dialog.getByRole("status")).toHaveText("Global note saved.");
});

test("pending global note saves keep their originating editor and reject navigation until completion", async ({
  page,
}) => {
  const id = "wb-global-async";
  await addBot(page, id);
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("button", { name: "Global notes", exact: true })
    .click();
  await dialog.getByLabel("Global note key", { exact: true }).fill("async");
  await dialog
    .getByLabel("Global note content", { exact: true })
    .fill("Originating editor note.");
  let release!: () => void;
  const wait = new Promise<void>((resolve) => {
    release = resolve;
  });
  let received = false;
  await page.route(`**/api/global-memory/${id}`, async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    received = true;
    await wait;
    return route.continue();
  });
  await dialog
    .getByRole("button", { name: "Save global note", exact: true })
    .click();
  await expect.poll(() => received).toBe(true);
  await navigate(page, "Providers");
  await expect(dialog.getByRole("alert")).toContainText(
    "Wait for the current save or action",
  );
  await expect(
    dialog.getByLabel("Global note content", { exact: true }),
  ).toHaveValue("Originating editor note.");
  release();
  await expect(dialog.getByRole("status")).toHaveText("Global note saved.");
  expect(
    (await (await page.request.get(`/api/global-memory/${id}`)).json()).notes[0]
      .value,
  ).toBe("Originating editor note.");
});
