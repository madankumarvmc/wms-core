// E2E smoke test for the Inventory Count task (bin<->coil mapping capture).
//
// Requires WMSLite Settings count_enabled=1 and WMS_USER to hold a count role
// (Loading Supervisor qualifies). Because the user also holds a loading role,
// the task picker is shown on a fresh context (no remembered task in IndexedDB).
//
// All data is __TEST__-prefixed; a bench-side cleanup removes it after the run.
const { test, expect } = require("@playwright/test");
const { login } = require("./helpers");

const BIN = "__TEST__BIN_E2E";
const COIL1 = "__TEST__COIL_E2E_1";
const COIL2 = "__TEST__COIL_E2E_2";

test("inventory count: pick task, scan bin, scan/QC/remove coil, save", async ({ page }) => {
  await login(page);
  await page.goto("/wms-loading", { waitUntil: "domcontentloaded" });

  // Task picker (user has both loading + count) — count tile present.
  await expect(page.locator("#screen-tasks")).toBeVisible();
  await expect(page.locator("#task-count")).toBeVisible();
  await page.locator("#task-count").click();

  // Bin-scan screen.
  await expect(page.locator("#screen-count-bin")).toBeVisible();
  await page.locator("#cbin-input").fill(BIN);
  await page.locator("#cbin-go").click();

  // Coil-capture screen; context shows the bin.
  await expect(page.locator("#screen-count")).toBeVisible();
  await expect(page.locator("#count-context")).toContainText(BIN);

  // Scan a coil -> a row appears; running count shows 1.
  await page.locator("#ccoil-input").fill(COIL1);
  await page.locator("#ccoil-go").click();
  const row1 = page.locator('.ccoil[data-coil="' + COIL1 + '"]');
  await expect(row1).toBeVisible();
  await expect(page.locator("#count-context")).toContainText("1 coil");

  // Toggle QC on that row.
  await row1.locator(".qc-toggle").click();
  await expect(row1.locator(".qc-toggle")).toHaveText("QC");
  await expect(row1).toHaveClass(/qc/);

  // Remove it -> row gone.
  await row1.locator(".rm").click();
  await expect(page.locator('.ccoil[data-coil="' + COIL1 + '"]')).toHaveCount(0);

  // Scan a second coil, then Save & next bin -> back to bin-scan screen.
  await page.locator("#ccoil-input").fill(COIL2);
  await page.locator("#ccoil-go").click();
  await expect(page.locator('.ccoil[data-coil="' + COIL2 + '"]')).toBeVisible();
  await page.locator("#cbtn-save").click();
  await expect(page.locator("#screen-count-bin")).toBeVisible();
});
