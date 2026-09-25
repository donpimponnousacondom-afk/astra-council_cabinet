import { expect, test } from "@playwright/test";

test("Secretary settings and ledger snooze/cancel controls are explicit and scoped", async ({
  page,
}, testInfo) => {
  // Isolated fixture server. Ledger responses are mocked; backend transactions
  // and auth are exercised in test_secretary.py. No live bot or provider calls.
  const alarm = {
    id: "alarm_01234567890123456789",
    bot_id: "ada",
    channel_id: "222222222222222222",
    key: "usage-review",
    message: "Remind root to review usage",
    due_at: "2030-01-01T21:00:00+01:00",
    repeat_seconds: 3600,
    state: "scheduled",
    revision: 1,
    last_turn_status: null,
  };
  await page.route("**/api/secretary", (route) =>
    route.fulfill({ json: [alarm] }),
  );
  await page.route(`**/api/secretary/${alarm.id}`, async (route) => {
    const body = route.request().postDataJSON();
    expect(body.revision).toBe(alarm.revision);
    if (body.operation === "snooze") {
      expect(body.after_seconds).toBe(7200);
      alarm.due_at = "2030-01-01T23:00:00+01:00";
    } else {
      expect(body.operation).toBe("cancel");
      alarm.state = "cancelled";
    }
    alarm.revision++;
    await route.fulfill({ json: alarm });
  });
  await page.goto("/");
  await page
    .getByLabel("Dashboard password")
    .fill("test-only-password-never-use-in-production");
  await page.getByRole("button", { name: "Enter council control" }).click();
  await expect(
    page.getByRole("heading", { name: "The council", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("navigation", { name: "Workbench pages" })
    .getByRole("button", { name: "Plugins", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Edit Secretary · experimental", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByLabel("Active reminders per bot (maximum)"),
  ).toHaveValue("100");
  await expect(
    dialog.getByLabel("Repeat interval (minimum seconds)"),
  ).toHaveValue("300");
  const ledger = dialog.getByRole("region", { name: "Secretary reminders" });
  await expect(ledger.getByText("Remind root to review usage")).toBeVisible();
  await ledger.getByLabel("Snooze for (minutes)").fill("0");
  await expect(
    ledger.getByRole("button", { name: "Snooze", exact: true }),
  ).toBeDisabled();
  await ledger.getByLabel("Snooze for (minutes)").fill("120");
  await ledger.getByRole("button", { name: "Snooze", exact: true }).click();
  await expect.poll(() => alarm.revision).toBe(2);
  await expect(ledger.getByText(/Repeats every 3600s/)).toBeVisible();
  await ledger.getByRole("button", { name: "Cancel reminder" }).click();
  await expect(
    ledger.getByRole("button", { name: "Cancel reminder" }),
  ).toBeDisabled();
  await expect(
    ledger.getByRole("button", { name: "Snooze", exact: true }),
  ).toBeEnabled();
  await page.screenshot({
    path: testInfo.outputPath("secretary-ledger.png"),
    fullPage: true,
  });
});
