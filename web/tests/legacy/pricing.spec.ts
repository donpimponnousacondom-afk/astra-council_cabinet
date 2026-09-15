import { test, expect } from "@playwright/test";

test("profile cache pricing persists separate rates and warns about incomplete estimates", async ({
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
    .fill("Cache pricing example");
  await dialog
    .getByLabel("Stable identifier", { exact: true })
    .fill("ui-cache-pricing");
  await dialog
    .getByLabel("Exact model identifier", { exact: true })
    .fill("example/vision");
  await dialog.getByText("Optional · price estimates", { exact: true }).click();
  await dialog
    .getByLabel("Flat input USD / million tokens", { exact: true })
    .fill("3");
  await dialog
    .getByLabel("Output USD / million tokens", { exact: true })
    .fill("15");
  await dialog
    .getByLabel("Cache-hit input USD / million tokens", { exact: true })
    .fill("0.014");
  await expect(
    dialog.getByText("Complete both cache rates", { exact: false }),
  ).toBeVisible();
  await dialog
    .getByLabel("Cache-miss input USD / million tokens", { exact: true })
    .fill("3");
  await expect(
    dialog.getByText("Complete both cache rates", { exact: false }),
  ).toHaveCount(0);
  await expect(dialog).toContainText(
    "Peak and off-peak schedules are not applied automatically.",
  );
  await dialog
    .getByRole("button", { name: "Create draft", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const profile = await (
    await page.request.get("/api/config/profiles/ui-cache-pricing")
  ).json();
  expect(profile.input_price_per_million).toBe(3);
  expect(profile.cache_hit_input_price_per_million).toBe(0.014);
  expect(profile.cache_miss_input_price_per_million).toBe(3);
  expect(profile.output_price_per_million).toBe(15);
  await page
    .getByRole("button", { name: "Edit Cache pricing example", exact: true })
    .click();
  await dialog.getByText("Optional · price estimates", { exact: true }).click();
  await expect(
    dialog.getByLabel("Cache-hit input USD / million tokens", { exact: true }),
  ).toHaveValue("0.014");
  await dialog
    .getByLabel("Cache-hit input USD / million tokens", { exact: true })
    .fill("");
  await dialog
    .getByLabel("Cache-miss input USD / million tokens", { exact: true })
    .fill("");
  await expect(
    dialog.getByText("Complete both cache rates", { exact: false }),
  ).toHaveCount(0);
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const flat = await (
    await page.request.get("/api/config/profiles/ui-cache-pricing")
  ).json();
  expect(flat.cache_hit_input_price_per_million).toBeNull();
  expect(flat.cache_miss_input_price_per_million).toBeNull();
  expect(flat.input_price_per_million).toBe(3);
});
