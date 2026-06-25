// Supervisor console: dashboard KPIs render, trucks list + drawer drill-down,
// SAP log, and the import view. Requires WMS_USER to hold Loading Supervisor.
// Seeds one plan (via SAP push) so the trucks list is non-empty.
const { test, expect } = require("@playwright/test");
const { login, seedPlan } = require("./helpers");

const TRUCK = "__TEST__E2E_CON_" + Date.now();

test("console dashboard, trucks drawer, sap log and import render", async ({ page }) => {
  await login(page);
  await page.goto("/wms-console", { waitUntil: "networkidle" });
  await seedPlan(page, {
    truck: TRUCK, planId: TRUCK,
    coils: [{ coil_barcode: "C-1", weight: 100 }, { coil_barcode: "C-2", weight: 200 }],
  });

  await expect(page.locator(".kpi").first()).toBeVisible();

  await page.click('.nav-item[data-v="trucks"]');
  await expect(page.locator("#t-table table.grid")).toBeVisible();
  await expect(page.locator("#t-table tbody tr").first()).toBeVisible();

  // drill into the first truck
  await page.locator("#t-table tr.click").first().click();
  await expect(page.locator("#drawer.show")).toBeVisible();
  await expect(page.locator("#drawer table.grid").first()).toBeVisible();
  await page.click("#scrim2");

  await page.click('.nav-item[data-v="saplog"]');
  await expect(page.locator("#content table.grid")).toBeVisible();

  await page.click('.nav-item[data-v="import"]');
  await expect(page.locator("#imp-file")).toBeVisible();
});
