# WMS Lite (`sbx_wmslite`)

Warehouse app for steel coils, built on Frappe — a StackBox product.

- **Truck loading:** SAP sends a loading plan (truck + coils). An operator scans each coil's QR
  on a handheld and confirms it loaded; completion can post a goods issue back to SAP.
- **Inventory Count:** scan a bin, then the coils in it, to map coils to locations.
- **RM Movement:** raw-material coils dispatched at the plant gate and received at a production line.
- **Inventory lookup:** a read-only "where is this coil / what is in this bin" screen.
- **Supervisor console:** dashboard, trucks, inventory, scan history, SAP logs, imports, users.
- Optional pushes of loading, count and RM-receipt scans to SAP.

| Page | Who | What |
| --- | --- | --- |
| `/wms-loading` | Operators | Handheld PWA (Zebra/DataWedge scanner or phone). Works offline. |
| `/wms-console` | Supervisors | Back-office console |
| `/wms-help` | Everyone | User manual |

## How this app runs

This repository holds **one Frappe app**, not a standalone program. It needs the
[Frappe Framework](https://frappeframework.com) underneath it:

```
bench  (a folder on the server, managed by the `bench` CLI)
├── apps/            ← code: frappe itself, this app, any other apps
│   ├── frappe
│   └── sbx_wmslite
└── sites/           ← data: one database per site
    └── your-site.example.com   ← the app is *installed into* a site
```

So setting it up is always the same three moves: **install Frappe with bench → create a
site → get this app and install it into that site.** One bench can serve several sites, and
several apps can share a bench.

ERPNext is **not** required.

## Requirements

Tested on this stack — stay on the same major versions:

| Component | Version |
| --- | --- |
| OS | Ubuntu 22.04 LTS |
| Python | 3.10 |
| Node.js | 18 or newer (production runs 22) + yarn 1.x |
| MariaDB | 10.6 |
| Redis | 6 |
| Frappe | **version-15** branch |
| bench CLI | 5.x |
| wkhtmltopdf | 0.12.6 with patched Qt (only for PDF printing) |

## Part 1 — Install Frappe (once per server)

Skip this part if the server already has a Frappe version-15 bench.

**1. System packages**

```bash
sudo apt update
sudo apt install -y git curl python3-dev python3-setuptools python3-pip python3.10-venv \
    software-properties-common mariadb-server mariadb-client redis-server \
    libmysqlclient-dev pkg-config xvfb libfontconfig
```

For PDF printing, install wkhtmltopdf 0.12.6 *with patched Qt* from the
[wkhtmltopdf releases](https://github.com/wkhtmltopdf/packaging/releases) (the apt package is unpatched).

**2. MariaDB for Frappe**

Frappe needs utf8mb4. Add this to `/etc/mysql/mariadb.conf.d/99-frappe.cnf`:

```ini
[mysqld]
character-set-client-handshake = FALSE
character-set-server = utf8mb4
collation-server = utf8mb4_unicode_ci

[mysql]
default-character-set = utf8mb4
```

Then restart it and set a root password:

```bash
sudo systemctl restart mariadb
sudo mysql_secure_installation
```

**3. Node.js and yarn** (as the user that will own the bench, not root)

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
source ~/.bashrc
nvm install 18
npm install -g yarn
```

**4. bench CLI**

```bash
pip3 install frappe-bench        # puts `bench` in ~/.local/bin — make sure that is on PATH
bench --version
```

**5. Create the bench with Frappe version-15**

```bash
cd ~
bench init --frappe-branch version-15 frappe-bench
cd frappe-bench
```

Every command below runs from inside this `frappe-bench` folder.

**6. Create a site**

```bash
bench new-site your-site.example.com      # prompts for the MariaDB root password and an Administrator password
bench --site your-site.example.com enable-scheduler
bench use your-site.example.com           # optional: makes it the default site
```

To try it locally, `bench start` serves the site on port 8000 (point `your-site.example.com`
at `127.0.0.1` in `/etc/hosts`, or use `localhost` as the site name).

## Part 2 — Install WMS Lite

```bash
cd ~/frappe-bench
bench get-app https://github.com/madankumarvmc/wms-core.git --branch rm-movement
bench --site your-site.example.com install-app sbx_wmslite
bench --site your-site.example.com migrate
bench build --app sbx_wmslite
bench restart
```

`rm-movement` is the current branch; `main` is behind it.

`install-app` also:

- creates the six roles and their permissions (below);
- seeds the location master (`WMSLite Bin`) from `sbx_wmslite/fixtures/bin_locations.txt`;
- seeds the site master (`WMSLite Site`) used by the console's Site filter.

Then open `https://your-site.example.com/wms-console` as Administrator.

## Part 3 — First-time configuration

**Users and roles.** Create users in Desk (User list) or from the console's **Users** view
(needs WMS Manager or System Manager).

| Role | Gets |
| --- | --- |
| Loading Operator | Handheld: Loading |
| Inventory Recorder | Handheld: Inventory Count |
| RM Gate Operator | Handheld: Gate Dispatch |
| RM Line Operator | Handheld: Line Receipt |
| Loading Supervisor | Console, and every handheld task |
| WMS Manager | Console, including user management |

Every handheld role also gets the read-only Inventory lookup card. A user with one task opens
straight into it; more than one shows a task picker.

**WMSLite Settings** (Desk → WMSLite Settings). Defaults are safe: everything that talks to SAP
starts **off**.

| Section | What to set |
| --- | --- |
| SAP Integration | `SAP API Key` — the shared secret SAP must send in the `X-WMSLite-Key` header |
| Coil QR Scanning | How a scanned coil QR is decoded into the coil's batch code |
| Bin-Directed Picking | Bin decode rule; `Validate Scanned Locations` refuses labels not in the location master |
| Inventory Count / RM Movement / Inventory Lookup | Switch each handheld task on or off |
| SAP zpwmsb Push (outbound) | SAP URL + Basic-auth user/password, then the per-feature push switches |
| SAP Goods Issue (outbound) | OAuth2 token URL, client, scope and goods-issue URL |
| Console Filters | Keywords that make a product count as "Pipes" |

**SAP → WMS endpoints** (all `POST`, authenticated by `X-WMSLite-Key`, not a login):

| Endpoint | Sends |
| --- | --- |
| `/api/method/sbx_wmslite.sap_api.receive_loading_plan` | A truck's loading plan |
| `/api/method/sbx_wmslite.bin_api.receive_bin_inventory` | Coil ↔ bin stock rows |
| `/api/method/sbx_wmslite.bin_api.receive_coil_transactions` | Individual scan transactions |

The payload contract is in `docs/WMSCore-SAP-LoadingPlan-API.docx`.

**Sites (console Site filter).** Desk → WMSLite Site lists each site with the bin-code prefixes
that belong to it (for example `JPR` or `CCL3`) and, for trucks, its SAP plant codes. Anything
not listed shows as "Unassigned".

**Scheduler.** Must be on: it retries SAP sends, derives inventory from new scans, and sweeps
pending pushes every 3–5 minutes.

## Going to production

```bash
sudo bench setup production $USER          # nginx + supervisor, workers, scheduler
bench config dns_multitenant on            # serve each site by its hostname
bench setup nginx && sudo systemctl reload nginx
sudo -H bench setup lets-encrypt your-site.example.com    # HTTPS (DNS must already point here)
```

Supervisor then runs gunicorn (web), the background workers and the scheduler. Confirm the
scheduler is on for the site: `bench --site your-site.example.com scheduler status`.

## Updating to a newer version of the app

Order matters:

```bash
cd apps/sbx_wmslite && git pull && cd ../..
bench --site your-site.example.com migrate     # schema + patches — FIRST
bench build --app sbx_wmslite                       # only if files under public/ changed
bench restart                                   # web + workers load the new Python code
```

- **Always restart after Python changes.** Production gunicorn preloads the code and the
  workers keep the old modules in memory; until a restart nothing changes.
- **Migrate before restarting.** New code may expect a column or doctype that only the
  migrate creates.
- **A restart briefly drops requests for every site on the bench.** Do it when the floor is
  quiet. The handheld apps queue scans offline and resend them, but a quiet moment is safer.

## Getting the code from a private repository

Never put a token in the remote URL (`https://<token>@github.com/...`): it is stored in plain
text in `.git/config` and printed by `git remote -v`. Use one of:

- **SSH (recommended):** add the server's SSH key to GitHub (or as a read-only deploy key on
  the repository), then use the `git@github.com:...` URL with `bench get-app`.
- **HTTPS with a credential helper:** `git config --global credential.helper store`, then enter
  a GitHub personal access token (not your password) when git asks.

## Which apps can share a site

WMS Lite (`sbx_wmslite`), Yard Management (`sbx_yms`) and Inventory Recording
(`inventory_recording`) can all live on **one bench**. On **one site**:

| Apps on the same site | OK? |
| --- | --- |
| WMS Lite + Yard Management | Yes |
| WMS Lite + Inventory Recording | Yes, but both use a role called **Inventory Recorder**: a user given it gets the recorder rights of both apps |
| Yard Management + Inventory Recording | **No.** Both define a doctype called `Import Batch`. A doctype name is site-wide, so whichever app migrates last takes over the table, and the other app's imports break. Put them on separate sites. |

## Running the tests

Run them on a **separate test site**, never on the live one: the suites change
WMSLite Settings for their duration and create and delete `__TEST__` data.

```bash
bench new-site test.example.com && bench --site test.example.com install-app sbx_wmslite
bench --site test.example.com set-config allow_tests true
bench --site test.example.com run-tests --app sbx_wmslite
bench --site test.example.com run-tests --app sbx_wmslite --module sbx_wmslite.tests.test_rm   # one suite
```

Two guards protect a live site if you point the suites at one by mistake:

- they refuse to start if operators scanned in the last 15 minutes
  (override with `WMSLITE_ALLOW_LIVE_TESTS=1` only if you know the floor is idle);
- the SAP-push suites refuse while real scans are queued for SAP. There is no override:
  a mocked SAP would mark those rows as sent.

Browser tests (Playwright) live in `tests/e2e/`.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| A code change does nothing | Run `bench restart`. Production preloads the code. |
| Handhelds still show the old screen | The PWA caches its shell. Bump `CACHE` in `sbx_wmslite/www/wms-loading-sw.js` on every handheld change; devices pick it up on their next online load. |
| A new setting reads as off although its default is on | A Single's defaults are only written when the form is saved, or by a patch. Open WMSLite Settings and save. |
| Every scan says "not a known location" | `Validate Scanned Locations` is on and the label is missing from the location master. Add it in the console's Bin Master view. |
| Console imports fail with an error from another app | Another app on the site defines the same doctype name. WMS Lite's batches use `WMSLite Import Batch` since patch v16; run `bench migrate`. |

## Documentation

- `docs/WMSLite-User-Manual.docx` and `/wms-help` — user manual
- `docs/WMSCore-SAP-LoadingPlan-API.docx` — SAP inbound contract
- `docs/WMSLite-Inventory-Count-Spec.md` — Inventory Count design
- `docs/WMSLite-RM-SAP-Push.md` — RM line receipt → SAP T2 push

## License

agpl-3.0
