import { expect, test } from "@playwright/test";

test("image inputs belong to the bot and survive editor saves without changing its shared profile", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByLabel("Dashboard password")
    .fill("test-only-password-never-use-in-production");
  await page.getByRole("button", { name: "Enter council control" }).click();
  await expect(
    page.getByRole("heading", { name: "The council", exact: true, level: 1 }),
  ).toBeVisible();
  const read = async (kind: string, id: string) =>
    (await page.request.get(`/api/config/${kind}/${id}`)).json();
  const before = await read("bots", "ada");
  const profile = await read("profiles", before.model_profile_id);
  const other = await read("bots", "hortator");
  await page
    .getByRole("navigation", { name: "Workbench pages" })
    .getByRole("button", { name: "Bots", exact: true })
    .click();
  await page.getByRole("button", { name: "Edit Ada", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("button", { name: "Capabilities", exact: true })
    .click();
  const toggle = dialog.getByRole("checkbox", {
    name: "Receive image inputs",
    exact: true,
  });
  await expect(toggle).toBeChecked();
  await toggle.uncheck();
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect((await read("bots", "ada")).allow_images).toBe(false);
  expect(await read("profiles", before.model_profile_id)).toEqual(profile);
  expect(await read("bots", "hortator")).toEqual(other);
  await page.getByRole("button", { name: "Edit Ada", exact: true }).click();
  await dialog
    .getByRole("button", { name: "Capabilities", exact: true })
    .click();
  await expect(toggle).not.toBeChecked();
  await toggle.check();
  await dialog
    .getByRole("button", { name: "Save changes", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect((await read("bots", "ada")).allow_images).toBe(true);
});
