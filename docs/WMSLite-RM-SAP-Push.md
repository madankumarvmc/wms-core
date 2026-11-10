# WMS Lite — RM line receipt → SAP T2 push

Built 2026-10-08 after the client reported that RM movement tracking was not
reaching SAP. Nothing had been built: RM Movement was a local-only ledger. This
note records what is pushed now and the three decisions the client made.

## Decisions (client, 2026-10-08)

1. **Only the line receipt is pushed.** A gate dispatch is not a location
   change (the coil is on a forklift), so SAP is not told about it.
2. **LOCATION = the destination line.** The receipt is the moment the coil's
   location becomes that line; `rm_receive` writes `bin_code` the same way.
3. **Undo behaves like the Inventory Count push.** Not yet sent → `Not
   Required`; already `Sent` → status kept, reconcile note appended, SAP is
   never told.

## The row

Same zpwmsb REST row as the T1 loading / count pushes, built by
`rm_push.build_rm_row`:

| Field | Value |
| --- | --- |
| TRANSACTIONTYPE | `rm_txn_type` setting, default **T2** |
| PLANT / PRODUCT / MATERIAL / SIZED / WEIGHT | from the scanned QR, else the coil's Bin Inventory row (SAP feed); MATERIAL falls back to `NA` |
| LOCATION | the destination line the coil was received at |
| BATCH | coil barcode |
| QC | `QC` if the coil's Bin Inventory row is on hold, else blank |
| ENTRYDATE / ENTRYTIME | the receipt's `scanned_at` (server time), `ddMMyyyy` / `HHmmss` |
| USERNAME | `loading_push_user` setting, default `jindal1` |

SAP de-dups on BATCH + ENTRYDATE + ENTRYTIME, so retries are safe.

## What is never pushed

`RM Dispatch`, both reversal rows, `RM Flag Cleared`, SAP feed rows, uploads.
A replayed offline receipt that was flagged `rm_unplanned` **is** pushed: the
coil is physically at the line, the flag is a WMS supervisor matter.

## Switches and operations

- **WMSLite Settings → RM Movement → "Push Line Receipts to SAP (T2)"**
  (`rm_push_enabled`, OFF by default) and "RM Receipt Transaction Type"
  (`rm_txn_type`). Host, credentials, SAP username, timeout, TLS and batch size
  are the shared ones under "SAP zpwmsb Push (outbound)".
- Receipts made while the switch is OFF park at `Disabled`;
  `rm_push.push_disabled` (or Push Now on the row) sends them later with their
  original timestamps.
- Receipts made **before this shipped keep a NULL status and are never pushed**
  (patch v13 does no backfill). Arm them by hand if the client wants them.
- Sweep: enqueued on every receipt, plus the 5-minute cron
  (`sbx_wmslite.rm_push.sweep`). Every attempt is logged in WMSLite SAP Log,
  action `RM Receipt`.
- Console scan feed: the SAP column and Retry button cover both pushed scan
  types (`console_api.retry_sap_push` routes on transaction type). Desk: Push
  Now on a Coil Transaction routes the same way. Workspace shortcut "SAP Scans
  Pending" counts both.

## Tests

`sbx_wmslite/tests/test_rm_push.py` (mocked HTTP; refuses to run while live
Pending pushes exist, see `settings_guard.assert_no_live_pending_pushes`).
`test_rm.py` and `test_locations.py` keep `rm_push_enabled=0` because they
call `rm_receive` without mocking SAP.
