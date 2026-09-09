import { test, expect } from "@playwright/test";

test("retained summary budget accepts 65536 and distinguishes provider caps", async ({
  page,
}) => {
  await page.goto("/legacy/");
  await page
    .getByLabel("Dashboard password")
    .fill("test-only-password-never-use-in-production");
  await page.getByRole("button", { name: "Enter council control" }).click();
  await page
    .getByRole("button", { name: "Model profiles", exact: true })
    .click();
  await page.getByRole("button", { name: "Add profile", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Display name", { exact: true })
    .fill("Reasoned compaction");
  await dialog
    .getByLabel("Stable identifier", { exact: true })
    .fill("reasoned-compaction");
  await dialog
    .getByLabel("Exact model identifier", { exact: true })
    .fill("fixture/reasoner");
  await dialog
    .getByLabel("Context window (tokens)", { exact: true })
    .fill("262144");
  const retained = dialog.getByLabel("Retained summary limit (tokens)", {
    exact: true,
  });
  await retained.fill("65536");
  expect(await retained.getAttribute("max")).toBeNull();
  await expect(dialog).toContainText("excluding private reasoning");
  const generation = {
    max_tokens: 2048,
    reasoning_effort: "high",
    vendor: { preserve: true },
  };
  await dialog
    .getByLabel("Model parameters", { exact: true })
    .fill(JSON.stringify(generation));
  await dialog
    .getByText("Advanced · compaction parameter overrides", { exact: true })
    .click();
  const overrides = {
    max_completion_tokens: 1024,
    thinking: { type: "enabled" },
  };
  await dialog
    .getByLabel("Compaction parameters", { exact: true })
    .fill(JSON.stringify(overrides));
  await expect(dialog).toContainText(
    "omitted from compaction requests, even if configured here",
  );
  await dialog
    .getByRole("button", { name: "Create draft", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const profile = await (
    await page.request.get("/api/config/profiles/reasoned-compaction")
  ).json();
  expect(profile.summary_tokens).toBe(65536);
  expect(profile.request_json).toEqual(generation);
  expect(profile.compaction_request_json).toEqual(overrides);
  const card = page.locator(".catalog-card").filter({
    has: page.getByRole("heading", {
      name: "Reasoned compaction",
      exact: true,
    }),
  });
  await expect(card).toContainText("65,536 text tokens");
  await expect(card).toContainText("Compaction total-output cap");
  await expect(card).toContainText("Not sent · provider default");
  await expect(card).toContainText("Generation output cap");
  await page
    .getByRole("button", { name: "Edit Reasoned compaction", exact: true })
    .click();
  await expect(retained).toHaveValue("65536");
  // A hard retention budget still has to leave room for the transcript and response.
  await retained.fill("262144");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog.getByRole("alert")).toContainText(
    "60% of the context window",
  );
  await retained.fill("65536");
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await card.scrollIntoViewIfNeeded();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: "test-results/compaction-budget-mobile.png",
    animations: "disabled",
  });
});
