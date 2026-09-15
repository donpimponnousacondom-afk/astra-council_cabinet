import { test, expect } from "@playwright/test";

test("published site runs local assets but cannot read dashboard cookies, DOM or APIs", async ({
  page,
}) => {
  await page.goto("/legacy/");
  await page
    .getByLabel("Dashboard password")
    .fill("test-only-password-never-use-in-production");
  await page.getByRole("button", { name: "Enter council control" }).click();
  await expect(
    page.getByRole("heading", { name: "The council", exact: true }),
  ).toBeVisible();
  expect((await page.request.get("/api/auth/session")).status()).toBe(200);
  const opened = page.waitForEvent("popup");
  await page.evaluate(() =>
    window.open("/sites/ada/sandbox-fixture/", "site-sandbox-preview"),
  );
  const site = await opened;
  await expect(
    site.getByRole("heading", { name: "Published sandbox fixture" }),
  ).toBeVisible();
  await expect
    .poll(() => site.evaluate(() => (window as any).fixtureReport ?? null), {
      timeout: 15000,
    })
    .not.toBeNull();
  const report = await site.evaluate(
    () => (window as any).fixtureReport ?? null,
  );
  expect(report).toMatchObject({
    origin: "null",
    openerDetached: true,
    inline: true,
    classic: true,
    module: true,
    image: true,
    css: true,
    data: true,
    openerBlocked: true,
    cookieBlocked: true,
    storageBlocked: true,
    apiBlocked: true,
    otherSiteBlocked: true,
    externalBlocked: true,
  });
  expect(
    report.violations.some((value: string) =>
      value.includes("/api/auth/session"),
    ),
  ).toBe(true);
  expect(
    await page.locator("body").getAttribute("data-compromised"),
  ).toBeNull();
  expect((await page.request.get("/api/auth/session")).status()).toBe(200);
  await site.close();
});
