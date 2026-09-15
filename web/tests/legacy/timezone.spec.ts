import { test, expect } from "@playwright/test";
import { dateLabel, setCouncilTimezone } from "../../src/legacy/api";

test("council dates preserve instants across midnight and seasonal offsets", () => {
  setCouncilTimezone("Europe/Madrid");
  expect(dateLabel("2026-09-08T23:46:00Z")).toBe("2026-09-09T01:46:00+02:00");
  expect(dateLabel("2026-01-08T23:46:00Z")).toBe("2026-01-09T00:46:00+01:00");
  setCouncilTimezone("Asia/Tokyo");
  expect(dateLabel(Date.parse("2026-09-08T23:46:00Z") / 1000)).toBe(
    "2026-09-09T08:46:00+09:00",
  );
  setCouncilTimezone("invalid timezone");
  expect(dateLabel("2026-09-08T23:46:00Z")).toBe("2026-09-09T01:46:00+02:00");
  expect(dateLabel(null)).toBe("—");
});

test.describe("a browser in another timezone", () => {
  test.use({ timezoneId: "America/Los_Angeles" });
  test("version presentation follows the council while source metadata stays UTC", async ({
    page,
  }) => {
    await page.route("**/api/status", async (route) => {
      const response = await route.fetch();
      const status = await response.json();
      status.settings.timezone = "Europe/Madrid";
      status.version.committed_at = "2026-09-08T23:46:00Z";
      await route.fulfill({ json: status });
    });
    await page.goto("/legacy/");
    await page
      .getByLabel("Dashboard password")
      .fill("test-only-password-never-use-in-production");
    await page.getByRole("button", { name: "Enter council control" }).click();
    const banner = page.getByRole("region", { name: "Running version" });
    await expect(banner.locator("time")).toHaveText(
      "2026-09-09T01:46:00+02:00",
    );
    await expect(banner.locator("time")).toHaveAttribute(
      "datetime",
      "2026-09-08T23:46:00Z",
    );
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(banner).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBeTruthy();
    await page.screenshot({
      path: "test-results/council-timezone-mobile.png",
      animations: "disabled",
    });
  });
});
