# SBX WMSLite — E2E tests

Playwright tests for the operator PWA (`/wms-loading`) and supervisor console
(`/wms-console`). They do **not** start a server; point them at a running site.

## Setup

```bash
cd apps/sbx_wmslite/tests/e2e
npm install
npx playwright install chromium
```

## Run

`WMS_USER` must hold **both** `Loading Operator` and `Loading Supervisor` roles.
`WMS_SAP_KEY` is the key stored in **WMSLite Settings** (the operator spec seeds
a plan through the SAP push endpoint). `allow_overscan` should be enabled in
settings for the over-scan assertion.

When the site only resolves on the box (e.g. `self.localhost.com` behind
nginx/gunicorn on loopback), set `HOST_MAP` so Chromium maps it to `127.0.0.1`:

```bash
BASE_URL=https://self.localhost.com \
HOST_MAP=self.localhost.com \
WMS_USER=wms_test@stackbox.xyz \
WMS_PWD='WmsTest#2026' \
WMS_SAP_KEY='TEST-KEY-123' \
npx playwright test
```

Test data is namespaced with the `__TEST__` prefix.
