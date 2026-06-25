// Shared E2E helpers. Credentials come from the environment so nothing is
// committed. WMS_USER must hold both Loading Operator and Loading Supervisor.
//
// Login is done via an in-browser fetch (not page.request) so that Chromium's
// --host-resolver-rules apply to the API call too.
const { expect } = require("@playwright/test");

const USR = process.env.WMS_USER;
const PWD = process.env.WMS_PWD;

async function login(page) {
  expect(USR && PWD, "Set WMS_USER / WMS_PWD").toBeTruthy();
  await page.goto("/login", { waitUntil: "domcontentloaded" });
  const ok = await page.evaluate(async (c) => {
    const r = await fetch("/api/method/login", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "usr=" + encodeURIComponent(c.usr) + "&pwd=" + encodeURIComponent(c.pwd),
    });
    return r.ok;
  }, { usr: USR, pwd: PWD });
  expect(ok, "login failed").toBeTruthy();
}

// Seed a loading plan via the SAP push endpoint. Must be called after the page
// has navigated to a WMSLite app page (which exposes window.WMS_CSRF) so the
// session POST passes Frappe's CSRF check; the endpoint then validates the key.
async function seedPlan(page, { truck, planId, coils }) {
  const key = process.env.WMS_SAP_KEY;
  expect(key, "Set WMS_SAP_KEY").toBeTruthy();
  const res = await page.evaluate(async (c) => {
    const r = await fetch("/api/method/sbx_wmslite.sap_api.receive_loading_plan", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-WMSLite-Key": c.key, "X-Frappe-CSRF-Token": window.WMS_CSRF },
      body: JSON.stringify({ sap_plan_id: c.planId, truck_number: c.truck, coils: c.coils }),
    });
    return { ok: r.ok, body: await r.json() };
  }, { key, planId, truck, coils });
  expect(res.ok, "SAP seed failed: " + JSON.stringify(res.body)).toBeTruthy();
}

// Seed Bin Inventory rows via the push endpoint (same key as the SAP feed).
// Each row: { coil_barcode, bin_code, zone, aisle, material_grade, weight }.
async function seedBins(page, rows) {
  const key = process.env.WMS_SAP_KEY;
  expect(key, "Set WMS_SAP_KEY").toBeTruthy();
  const res = await page.evaluate(async (c) => {
    const r = await fetch("/api/method/sbx_wmslite.bin_api.receive_bin_inventory", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-WMSLite-Key": c.key, "X-Frappe-CSRF-Token": window.WMS_CSRF },
      body: JSON.stringify({ action: "upsert", rows: c.rows }),
    });
    return { ok: r.ok, body: await r.json() };
  }, { key, rows });
  expect(res.ok, "bin seed failed: " + JSON.stringify(res.body)).toBeTruthy();
}

module.exports = { login, seedPlan, seedBins };

