# WMS Core — Inventory Count (Bin Mapping) — Product & Engineering Spec

**Status:** Draft for review
**Author:** (drafted with Claude Code)
**Date:** 2026-07-09
**App:** `sbx_wmslite` (Frappe) · HHT PWA at `/wms-loading`

---

## 1. Summary

Add an **Inventory Count** task to the WMS Core handheld (HHT) app. Despite the
name, the MVP is a **bin↔coil mapping capture**, not a stock reconciliation: an
operator walks to a bin, scans the **bin/location barcode**, then scans each
**coil QR** present in that bin and saves. The app records, for each coil, which
bin it is in (plus QC status), which updates the existing **Bin Inventory** truth
(latest-scan-wins). It does **not** compute variance / missing coils in the MVP.

This mirrors the reference AppSheet "Batch Mapping" screens (Location Barcode →
Add Coil Batch Codes → per-coil Inventory Barcode + auto Batch Code + QC OK/QC →
Save), but with a **lower-touch, scan-and-go flow** (see §5).

The feature is **permission-gated**:

| Role | Sees |
|---|---|
| Loading Operator | Loading only |
| Inventory Recorder *(new)* | Inventory Count only |
| Both roles / Loading Supervisor / System Manager | A **task picker** to choose Loading or Inventory Count |

> **Finalized scope decisions (2026-07-09):** MVP = capture/mapping only (no
> reconciliation); **no** `Inventory Count Session` doctype in MVP; coils on an
> open loading plan are recorded independently with **no check/warning**; new
> role is **`Inventory Recorder`**.

---

## 2. Why this is a small build (reuse, not rebuild)

The data model and intake pipeline this feature needs **already exist** and were
clearly designed with it in mind:

- **`Bin Inventory`** (`sbx_wmslite/doctype/bin_inventory/`) maps
  `coil_barcode → bin_code` with `status`, `qc` (hold flag), `material_grade`,
  `weight`, `zone`, `aisle`, `plant`, `product`, `sized`, `scanned_at`, and a
  **`source` field whose options are `Push API | Upload | In-app`.** The
  `In-app` source is already anticipated — Inventory Count is what fills it.
- **`Coil Transaction`** (`sbx_wmslite/doctype/coil_transaction/`) is an
  append-only scan ledger with idempotency key `txn_key`, a `source`
  (`Push API | Upload | In-app`), `transaction_type`, `username`, `processed`
  flag, and a link back to `bin_inventory`.
- **`bin_api.process_pending_transactions()`** derives `Bin Inventory` from
  unprocessed `Coil Transaction` rows, **oldest-scan-first so the latest scan
  wins**, and marks each processed. It runs immediately after intake (enqueued)
  and on a `*/5 * * * *` scheduler sweep (`hooks.py`).
- **`bin_api.upsert_bin()`** already does a safe latest-scan-wins upsert that
  **only overwrites non-empty fields** — so an in-app scan that carries only
  `coil + bin + timestamp` updates the location without wiping the grade/weight
  the SAP feed supplied.

**Consequence:** Inventory Count is a *new producer* of `Coil Transaction` rows
with `source = "In-app"`, plus a new HHT screen. The reconciliation, idempotency,
latest-wins, and scheduler safety-net are all reused unchanged.

The client-side plumbing is also reusable from `www/wms-loading.html`: the
IndexedDB offline queue (`enqueue`/`syncQueue`, idempotent by `client_event_id`),
the DataWedge burst **scanner auto-detect** (`autoScanWatch`), the `api()` helper
with CSRF, the barcode decode seam (`decode.py` server-side, `decodeInventoryBarcode`
/ `decodeBinLocal` client-side), config caching (`get_client_config`), and the
service worker.

---

## 3. Scope & phasing (product-manner rollout)

Ship behind a `count_enabled` flag in **WMSLite Settings**.

### Phase 1 — Capture / mapping — *MVP (this build)*
Scan bin → scan coils → save. Each scanned coil's current bin is updated to the
scanned bin (latest-wins, exactly like the push feed). QC OK/hold captured per
coil. **No variance logic, no session record, no plan checks** — coils are
recorded independently of any loading plan. Fully offline-capable. This delivers
the stated requirement.

### Phase 2 — Reconciliation (cycle count) — *future, optional*
On finishing a bin, compare the **scanned set** against what `Bin Inventory`
currently believes is in that bin (found/missing/moved-in), backed by an
`Inventory Count Session` record with variance counts. Deferred — not in scope
now, documented so the MVP data (`Coil Transaction` rows) stays forward-compatible.

### Phase 3 — Console & reporting — *future, optional*
Supervisor view in `www/wms-console.html`: per-bin history, export. Can reuse the
existing `console_api.get_bin_inventory` listing.

---

## 4. Permissions & task selection

### 4.1 Roles
Add a third role in `setup_roles.py` alongside `Loading Operator` /
`Loading Supervisor`:

```
INVENTORY = "Inventory Recorder"
ROLE_PERMS[INVENTORY] = {
    "Coil Transaction":        {"read": 1, "write": 1, "create": 1},
    "Bin Inventory":           {"read": 1, "write": 1},
    "WMSLite Settings":        {"read": 1},
}
```
`Loading Supervisor` and `System Manager` implicitly get both tasks (superuser).

### 4.2 Server-side enforcement (not just UI hiding)
Every new whitelisted endpoint must assert the caller's role — UI gating is a
convenience, not a security boundary. Add a guard:

```python
def _require_count_role():
    roles = set(frappe.get_roles())
    if not (roles & {"Inventory Recorder", "Loading Supervisor", "System Manager"}):
        frappe.throw("Not permitted", frappe.PermissionError)
```

### 4.3 Task picker
New endpoint `my_tasks()` returns the tasks the user may perform:
```json
{ "tasks": ["loading", "count"], "default": null }
```
- 0 tasks → show a "no tasks assigned — contact your supervisor" message.
- 1 task → **skip the picker**, land directly on that task (less touch).
- 2 tasks → show a two-button task chooser as the app landing screen; the choice
  is remembered (IndexedDB `kv`) so a returning user lands where they left off,
  with a persistent "Switch task" affordance in the top bar.

---

## 5. The count flow (lower-touch redesign)

The reference AppSheet flow costs ~3 taps per coil (New → scan → Save) plus a
final Save. We collapse that to **scan-and-go**:

```
[Task picker]  (only if user has both tasks)
      │  tap "Inventory Count"
      ▼
┌─────────────────────────────────────────┐
│  Scan / type BIN barcode          [→]    │   ← one bin scan opens a session
└─────────────────────────────────────────┘
      │  bin decoded (decode_bin_code)
      ▼
┌─────────────────────────────────────────┐
│  BIN CC2/G13            12 coils · 1 QC   │   ← sticky context + running count
│  ┌─────────────────────────────────────┐ │
│  │ Scan coil                      [→]   │ │   ← input stays focused; scan → beep
│  └─────────────────────────────────────┘ │      → row appears → ready for next
│  2R5H34419   Grade X · OK        [QC][✕] │   ← default OK; tap QC only if hold
│  2R5H34421   Grade X · OK        [QC][✕] │   ← [✕] removes a mis-scan
│  … newest on top …                        │
│                                           │
│  [ Save & next bin ]                      │
└─────────────────────────────────────────┘
```

Key touch-savers:
1. **No per-coil "New"/"Save".** A coil scan is captured and persisted
   immediately (queued offline-first, sent to `count_scan`). The input
   auto-refocuses for the next scan. Loading already proves this pattern with
   `autoScanWatch`. So there is **no "lose your work"** — closing the app mid-bin
   loses nothing.
2. **Batch/coil code auto-derived** from the QR (`decode_coil_id` /
   `decodeInventoryBarcode`) — no manual entry, no dropdown. (In the AppSheet
   screen "Batch Code" is a derived, read-only field; we keep it derived.)
3. **QC defaults to OK.** Only tap the row's `QC` toggle for a hold — the common
   case is zero taps.
4. **"Save & next bin"** is the explicit finish action the operator expects: it
   flushes any queued scans (when online) and returns to the bin-scan input so
   the next bin is one scan away. It is a *confirmation/close*, not a
   commit-or-lose — the coils are already recorded scan-by-scan.
5. Haptic + banner feedback on every scan (reuse `banner()` + `navigator.vibrate`).

---

## 6. Backend design

### 6.1 New whitelisted endpoints (`api.py` or a new `count_api.py`)
All call `_require_count_role()` first.

| Endpoint | Purpose |
|---|---|
| `my_tasks()` | Tasks the current user may perform (for the picker). |
| `count_open_bin(raw_bin)` | Decode bin, return existing coils recorded in this bin (resume support) + bin metadata. |
| `count_scan(bin_code, raw_qr, client_event_id, qc=0)` | Decode coil; create/update a `Coil Transaction` (`source="In-app"`, `transaction_type="Inventory Count"`); derive immediately; return `{coil_id, previous_bin, moved, qc, existing}`. |
| `count_set_qc(bin_code, coil_barcode, qc)` | Toggle QC hold on a just-scanned coil. |
| `count_remove(bin_code, coil_barcode, client_event_id)` | Undo a mis-scan in the current session. |
| `count_finish(bin_code)` | "Save & next bin" — flush/confirm; no variance in MVP (Phase 2 adds it). May be a no-op server-side if all scans already synced. |
| Extend `get_client_config()` | Add `count_enabled`, `count_qc_default`, and reuse `bin_decode_*` / `qr_decode_*`. |
| Extend `submit_offline_queue()` | Handle event types `count_scan`, `count_remove`. |

### 6.2 `count_scan` semantics
- **Timestamp is server-side** (`now_datetime()`), *not* the device clock —
  device clock skew must never win the latest-scan-wins race. (The push feed
  uses SAP `ENTRYDATE/ENTRYTIME`; in-app uses server time.)
- **Idempotency:** `txn_key = client_event_id` (client-generated `uuid()`), so an
  offline replay never double-inserts. Distinct from the push feed's
  `coil_date_time` key — both coexist in `Coil Transaction.txn_key`.
- **Only sets location fields** (`bin_code`, `scanned_at`, `qc`, `username`) —
  never blanks `material_grade`/`weight` (guaranteed by `upsert_bin`'s
  non-empty-field rule). A brand-new coil (not in `Bin Inventory`) is created
  with a blank grade — acceptable.
- Returns `previous_bin` and `moved` so the UI can show "moved from CC1/G07".

### 6.3 New doctype (Phase 2) — `Inventory Count Session`
Parent record grouping one bin count for audit + reconciliation:
`bin_code`, `counted_by`, `started_at`, `finished_at`,
`status (Open|Finished|Reviewed)`, `expected_count`, `found_count`,
`missing_count`, `moved_in_count`, and a child table or link of the coils.
Phase 1 can defer this and group by `Coil Transaction.username + scanned_at`.

---

## 7. Frontend design

### 7.1 App shell
Keep **one PWA** (`/wms-loading`). Rationale: shared scanner/offline/decode/config
plumbing, and superusers switch tasks in-session. Add:
- A `screen-tasks` landing (task picker) shown per §4.3.
- A `screen-count-bin` (bin scan) and `screen-count` (coil capture) section,
  mirroring the existing `screen-*` show/hide pattern in `showScreen()`.
- Rename the app title context to neutral "WMS Core" (already done in git:
  "Rebrand UI to WMS Core").

> Alternative considered: a separate `www/wms-count.html` page. Rejected for
> Phase 1 — it would duplicate the offline queue, scanner detection, and config
> boot. Revisit only if the two flows diverge heavily.

### 7.2 Offline queue
Reuse IndexedDB. New event types:
- `count_scan` → `{type, bin_code, coil_barcode, raw_qr, qc, client_event_id}`
- `count_remove` → `{type, bin_code, coil_barcode, client_event_id}`
Optimistic local update on scan (row appears instantly), then server sync;
on failure, `enqueue()` and replay via `syncQueue()` — identical to `confirm_load`.

### 7.3 Bin decode / coil decode
Reuse `decodeBinLocal` (config `bin_decode_rule/regex`) for the bin barcode and
`decodeInventoryBarcode` (the ported Jindal AppSheet formula) for the coil QR, so
the derived batch code matches exactly what the SAP/push feed produces.

---

## 8. Edge cases (comprehensive)

**Input / decode**
- Empty or whitespace bin scan → ignore, keep focus.
- Bin barcode fails decode / regex has no match → fall back to raw (mirror
  `decode_bin_code`), show the raw value so the operator can verify.
- Coil QR undecodable / garbage → still record raw as coil id? No — show a
  "couldn't read coil" banner and skip, to avoid junk `Bin Inventory` rows.
- Bin barcode accidentally scanned into the coil field (or vice-versa) → if a
  scanned "coil" decodes to a known bin code, warn "that's a bin barcode".

**Duplicates / repeats**
- Same coil scanned twice in one bin session → dedupe; show "already counted in
  this bin", no double row, gentle haptic.
- Coil already recorded in this **same** bin from a prior session → mark
  `existing`, refresh `scanned_at`, no error.

**Moves & conflicts**
- Coil currently in a **different** bin → this scan moves it (latest-wins); banner
  "moved from CC1/G07" (informational).
- Coil is on an **open loading plan** / already `status = Picked` or `Loaded` →
  **no check** — record independently (per finalized decision). The mapping simply
  reflects the physical scan.
- Two operators counting the **same bin** concurrently → both write transactions;
  latest-wins converges. No locking in MVP.

**Timing / data integrity**
- **Device clock skew** → server timestamps only (§6.2).
- Offline scan then coil moved by someone else online before replay → replay uses
  the scan's queued time; latest-wins may or may not apply. Acceptable; document
  that online counts supersede stale offline replays only by timestamp.
- App closed mid-bin → nothing lost (each scan persisted). On reopen, `count_open_bin`
  repopulates the in-progress list.

**QC**
- QC hold toggled after scan → `count_set_qc` updates the row's `qc`; reflected in
  `Bin Inventory.qc`. (Maps to AppSheet OK/QC.)
- QC hold coil counted → still counted; `qc=1` is informational, does not block.

**Permissions**
- User with neither role hits the page → task picker shows "no tasks"; endpoints
  still 403 server-side.
- Role revoked mid-session → next endpoint call 403s; UI shows "session ended".

**Scale / performance**
- Bin with hundreds of coils → cap the rendered list (newest N) with a count
  header; the full set lives server-side. Avoid re-rendering the whole list on
  every scan (append the one new row).
- Offline queue backlog large → `submit_offline_queue` already batches; keep it.

**Bin / location**
- Unknown bin (no prior inventory) → allowed; creates fresh location mappings.
- `Blocked` bin status → warn but allow (physical count is ground truth).

---

## 9. Configuration (WMSLite Settings additions)
New section `section_count`:
- `count_enabled` (Check) — master feature flag.
- `count_qc_default` (Select OK/QC) — default per-coil QC state (default OK).
- `count_block_if_on_open_plan` (Check) — hard-block counting coils on an open
  loading plan (default off).
- Reuse existing `bin_decode_rule/regex` and `qr_decode_rule/regex`.

---

## 10. Delivery plan

1. **Roles & settings** — add `Inventory Operator` in `setup_roles.py`; add
   settings fields; migration/patch entry in `patches.txt`.
2. **Backend** — `count_api.py` with the §6.1 endpoints + `_require_count_role`;
   extend `get_client_config` and `submit_offline_queue`. Reuse `upsert_bin` /
   `process_pending_transactions`.
3. **Frontend** — task picker + bin/coil count screens in `wms-loading.html`;
   new offline event types; reuse scanner/decode/queue.
4. **Phase 2** — `Inventory Count Session` doctype + `count_finish` variance.
5. **Phase 3** — console report in `wms-console.html`.
6. **Tests** — mirror `tests/`: idempotent `count_scan`, latest-wins move,
   offline replay dedupe, role enforcement (403), QC toggle, duplicate-in-session.
7. **Docs** — update `docs/WMSLite-User-Manual.docx` with the count task.

---

## 11. Decisions (resolved 2026-07-09)
1. **MVP semantics:** capture / bin↔coil mapping only. No reconciliation. ✅
2. **New role name:** `Inventory Recorder`. ✅
3. **Coils on an open loading plan:** no check — record independently. ✅
4. **`Inventory Count Session` doctype:** not in MVP; deferred to Phase 2. ✅

## 12. Config additions — trimmed for MVP
Given the resolved scope, MVP needs only:
- `count_enabled` (Check) — master feature flag.
- `count_qc_default` (Select OK/QC) — default per-coil QC (default OK).
- Reuse existing `bin_decode_rule/regex` and `qr_decode_rule/regex`.

(`count_block_if_on_open_plan` from §9 is dropped — no plan check in MVP.)
