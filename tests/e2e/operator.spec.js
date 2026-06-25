// Operator PWA flow: SAP push seeds a plan -> operator finds truck, claims,
// scans + confirms a coil, hits over-scan + already-loaded paths.
//
// Self-seeds via the SAP push endpoint, so set WMS_SAP_KEY to the key stored in
// WMSLite Settings (in addition to WMS_USER / WMS_PWD). allow_overscan must be on.
const { test, expect } = require("@playwright/test");
const { login, seedPlan } = require("./helpers");

const TRUCK = "__TEST__E2E_OP_" + Date.now();

test("operator loads a coil and hits exception paths", async ({ page }) => {
  await login(page);
  await page.goto("/wms-loading", { waitUntil: "networkidle" });
  await seedPlan(page, {
    truck: TRUCK, planId: TRUCK,
    coils: [
      { coil_barcode: "E2E-1", material_grade: "GR50", weight: 5000 },
      { coil_barcode: "E2E-2", material_grade: "GR50", weight: 4800 },
      { coil_barcode: "E2E-3", material_grade: "GR40", weight: 5200 },
    ],
  });

  await page.fill("#truck-input", TRUCK);
  await page.press("#truck-input", "Enter");
  await page.click("#truck-results .sb-card");
  await expect(page.locator("#screen-coils")).toBeVisible();
  await expect(page.locator("#c-total")).toHaveText("3");

  // scan + confirm
  await page.fill("#coil-input", "E2E-1");
  await page.press("#coil-input", "Enter");
  await page.click("#do-confirm");
  await expect(page.locator("#c-loaded")).toHaveText("1");

  // already-loaded reject
  await page.fill("#coil-input", "E2E-1");
  await page.press("#coil-input", "Enter");
  await expect(page.locator("#banner")).toContainText(/already loaded/i);

  // unknown coil -> over-scan prompt
  await page.fill("#coil-input", "UNKNOWN-XYZ");
  await page.press("#coil-input", "Enter");
  await expect(page.locator("#do-over")).toBeVisible();
});
