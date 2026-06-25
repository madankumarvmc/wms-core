// Playwright config for the SBX WMSLite E2E suite.
//
// The app must already be running and reachable at BASE_URL. When the site is
// only resolvable on the box itself (e.g. self.localhost.com -> gunicorn/nginx
// on loopback), set HOST_MAP to have Chromium map the hostname to 127.0.0.1:
//
//   BASE_URL=https://self.localhost.com HOST_MAP=self.localhost.com \
//   WMS_USER=wms_test@stackbox.xyz WMS_PWD='...' npx playwright test
//
// WMS_USER must have BOTH Loading Operator and Loading Supervisor roles.
const { defineConfig, devices } = require("@playwright/test");

const hostMap = process.env.HOST_MAP;
const launchArgs = hostMap ? [`--host-resolver-rules=MAP ${hostMap} 127.0.0.1`] : [];

module.exports = defineConfig({
  testDir: ".",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false, // specs mutate shared rows; keep sequential
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.BASE_URL || "https://self.localhost.com",
    ignoreHTTPSErrors: true,
    headless: true,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    launchOptions: { args: launchArgs },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
