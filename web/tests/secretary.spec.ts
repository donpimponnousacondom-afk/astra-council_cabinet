import { expect, test, type Page } from "@playwright/test";

async function openSecretary(page: Page) {
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
  return page.getByRole("dialog");
}

test("Secretary settings and owner ledger edits, snooze and cancellation", async ({
  page,
}, testInfo) => {
  // Isolated fixture server. Ledger responses are mocked; backend transactions
  // and auth are exercised in test_secretary.py. No live bot or provider calls.
  const alarm = {
    id: "alarm_01234567890123456789",
    bot_id: "ada",
    channel_id: "222222222222222222",
    destination: { kind: "owner_dm", label: "Private DM to owner" },
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
    } else if (body.operation === "update") {
      expect(body.message).toBe("Review usage and compare providers");
      expect(body.repeat_seconds).toBe(0);
      alarm.message = body.message;
      alarm.repeat_seconds = body.repeat_seconds;
    } else {
      expect(body.operation).toBe("cancel");
      alarm.state = "cancelled";
    }
    alarm.revision++;
    await route.fulfill({ json: alarm });
  });
  const dialog = await openSecretary(page);
  await expect(
    dialog.getByLabel("Active reminders per bot (maximum)"),
  ).toHaveValue("100");
  await expect(
    dialog.getByLabel("Repeat interval (minimum seconds)"),
  ).toHaveValue("300");
  const ledger = dialog.getByRole("region", { name: "Secretary reminders" });
  await expect(ledger.getByText("Remind root to review usage")).toBeVisible();
  await expect(ledger.getByText(/Private DM to owner/)).toBeVisible();
  await ledger.getByRole("button", { name: "Edit reminder" }).click();
  await ledger.getByLabel("Reminder message").fill(" ");
  await expect(
    ledger.getByRole("button", { name: "Save reminder" }),
  ).toBeDisabled();
  await ledger
    .getByLabel("Reminder message")
    .fill("Review usage and compare providers");
  await ledger.getByLabel("Repeat every (seconds; 0 = one-off)").fill("0");
  await expect(
    ledger.getByRole("button", { name: "Refresh reminders" }),
  ).toBeDisabled();
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(
    dialog.getByText(/Save or discard the pending reminder edit first/),
  ).toBeVisible();
  const dismiss = async (popup: import("@playwright/test").Dialog) => {
    expect(popup.message()).toContain("Discard unsaved changes");
    await popup.dismiss();
  };
  page.once("dialog", dismiss);
  await page.keyboard.press("Escape");
  await expect(ledger.getByLabel("Reminder message")).toHaveValue(
    "Review usage and compare providers",
  );
  await ledger.getByRole("button", { name: "Save reminder" }).click();
  await expect(ledger.getByLabel("Reminder message")).toHaveCount(0);
  expect(alarm.revision).toBe(2);
  expect(alarm.due_at).toBe("2030-01-01T21:00:00+01:00");
  expect(alarm.state).toBe("scheduled");
  await ledger.getByLabel("Snooze for (minutes)").fill("0");
  await expect(
    ledger.getByRole("button", { name: "Snooze", exact: true }),
  ).toBeDisabled();
  await ledger.getByLabel("Snooze for (minutes)").fill("120");
  await ledger.getByRole("button", { name: "Snooze", exact: true }).click();
  await expect.poll(() => alarm.revision).toBe(3);
  await expect(ledger.getByText(/One-off/)).toBeVisible();
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

test("Secretary preserves edits on a stale revision and guards an in-flight save", async ({
  page,
}) => {
  const alarm = {
    id: "alarm_01234567890123456789",
    bot_id: "ada",
    channel_id: "222222222222222222",
    key: "usage-review",
    message: "Original reminder",
    due_at: "2030-01-01T21:00:00+01:00",
    repeat_seconds: 3600,
    state: "scheduled",
    revision: 1,
    last_turn_status: null,
  };
  await page.route("**/api/secretary", (route) =>
    route.fulfill({ json: [alarm] }),
  );
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  let requested = false;
  await page.route(`**/api/secretary/${alarm.id}`, async (route) => {
    expect(route.request().postDataJSON().revision).toBe(1);
    requested = true;
    await pending;
    await route.fulfill({
      status: 409,
      json: { error: "Reminder changed; refresh before editing" },
    });
  });
  const dialog = await openSecretary(page);
  const ledger = dialog.getByRole("region", { name: "Secretary reminders" });
  await ledger.getByRole("button", { name: "Edit reminder" }).click();
  await ledger.getByLabel("Reminder message").fill("My unsaved correction");
  await ledger.getByRole("button", { name: "Save reminder" }).click();
  await expect.poll(() => requested).toBe(true);
  await expect(
    ledger.getByRole("button", { name: "Discard edit" }),
  ).toBeDisabled();
  await dialog.getByRole("button", { name: "Close dialog" }).click();
  await expect(
    dialog.getByText(/Wait for the current save or action to finish/),
  ).toBeVisible();
  release();
  await expect(
    ledger.getByText(/Reminder changed; refresh before editing/),
  ).toBeVisible();
  await expect(ledger.getByLabel("Reminder message")).toHaveValue(
    "My unsaved correction",
  );
  alarm.message = "Loki's newer reminder";
  alarm.revision = 2;
  await ledger.getByRole("button", { name: "Discard edit" }).click();
  await ledger.getByRole("button", { name: "Refresh reminders" }).click();
  await expect(ledger.getByText("Loki's newer reminder")).toBeVisible();
  await expect(ledger.getByLabel("Reminder message")).toHaveCount(0);
  await dialog.getByRole("button", { name: "Close dialog" }).click();
  await expect(dialog).toHaveCount(0);
});
