// Bin-directed picking: SAP push seeds a plan, the bin feed locates the coils,
// and the operator works the truck bin-by-bin (bins-to-visit -> open a bin ->
// scan its coils -> confirm). Also covers the wrong-bin guard and the
// "Location unknown" group for coils with no bin.
//
// Precondition: WMSLite Settings must have bin_picking_enabled = 1 and
// allow_pick_outside_bin = 0 (strict bin-first). Set WMS_USER / WMS_PWD /
// WMS_SAP_KEY as for the other specs.
const { test, expect } = require("@playwright/test");
const { login, seedPlan, seedBins } = require("./helpers");

const TRUCK = "__TEST__E2E_BIN_" + Date.now();
const C = (n) => "BINQA-" + TRUCK.slice(-6) + "-" + n;

test("operator picks a truck bin-by-bin", async ({ page }) => {
  await login(page);
  await page.goto("/wms-loading", { waitUntil: "networkidle" });

  // 3 coils: two in BIN-A1 (Zone A/Aisle 1), one with no bin (Location unknown)
  await seedPlan(page, {
    truck: TRUCK, planId: TRUCK,
    coils: [
      { coil_barcode: C(1), material_grade: "GR50", weight: 5000 },
      { coil_barcode: C(2), material_grade: "GR50", weight: 4800 },
      { coil_barcode: C(3), material_grade: "GR40", weight: 5200 },
    ],
  });
  await seedBins(page, [
    { coil_barcode: C(1), bin_code: "BIN-A1", zone: "A", aisle: "1" },
    { coil_barcode: C(2), bin_code: "BIN-A1", zone: "A", aisle: "1" },
    // C(3) intentionally has no bin -> "Location unknown"
  ]);

  // open the truck
  await page.fill("#truck-input", TRUCK);
  await page.press("#truck-input", "Enter");
  await page.click("#truck-results .sb-card");
  await expect(page.locator("#screen-coils")).toBeVisible();

  // bins-to-visit shown (bin mode active because the truck has located coils)
  await expect(page.locator("#bin-nav")).toBeVisible();
  await expect(page.locator("#bin-list .bincard")).toHaveCount(2); // BIN-A1 + Location unknown
  await expect(page.locator("#bin-list")).toContainText("BIN-A1");
  await expect(page.locator("#bin-list")).toContainText("Location unknown");

  // open BIN-A1 by typing its code
  await page.fill("#bin-input", "BIN-A1");
  await page.press("#bin-input", "Enter");
  await expect(page.locator("#bin-context")).toBeVisible();
  await expect(page.locator("#coil-list .coil")).toHaveCount(2);

  // wrong-bin guard: C(3) is unmapped, not in BIN-A1 -> blocked
  await page.fill("#coil-input", C(3));
  await page.press("#coil-input", "Enter");
  await expect(page.locator("#banner")).toContainText(/not this bin/i);
  await expect(page.locator("#armed")).toBeHidden();

  // correct coils -> arm + confirm both
  for (const code of [C(1), C(2)]) {
    await page.fill("#coil-input", code);
    await page.press("#coil-input", "Enter");
    await expect(page.locator("#ac-confirm")).toBeVisible();
    await page.click("#ac-confirm");
  }

  // bin fully picked -> auto-return to bins, BIN-A1 marked done
  await expect(page.locator("#bin-nav")).toBeVisible();
  await expect(page.locator("#bin-list .bincard.done")).toContainText("BIN-A1");
  await expect(page.locator("#c-loaded")).toHaveText("2");

  // the unknown coil is still pickable from its group
  await page.click("#bin-list .bincard.unknown");
  await expect(page.locator("#bin-context")).toContainText("Location unknown");
  await page.fill("#coil-input", C(3));
  await page.press("#coil-input", "Enter");
  await expect(page.locator("#ac-confirm")).toBeVisible();
  await page.click("#ac-confirm");
  await expect(page.locator("#c-loaded")).toHaveText("3");
});
