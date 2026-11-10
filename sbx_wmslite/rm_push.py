"""Outbound SAP push for RM Movement line receipts (T2).

When a line operator receives a coil (rm_api.rm_receive), SAP is told the coil
is now at that production line: one T2 transaction over the zpwmsb REST
interface, in exactly the row shape the T1 loading / count pushes use, with
LOCATION = the destination line the coil was received at. Transport, CSRF
handshake and response matching are shared with loading_push.post_rows.

Agreed with the client 2026-10-08:
  * only the LINE RECEIPT is pushed — a gate dispatch is not a location change
    (the coil is on a forklift) and SAP is not told about it;
  * LOCATION carries the destination line, because the receipt is the moment
    the coil's location becomes that line (rm_receive writes bin_code the same
    way);
  * undo behaves like the Inventory Count push: not yet sent -> "Not Required",
    already Sent -> status kept with a reconcile note, SAP is NOT told.

What is pushed — and what never is:
  * ONLY Coil Transaction rows with transaction_type "RM Receipt" AND source
    "In-app". A replayed offline receipt that was flagged `rm_unplanned` is
    still a receipt — the coil is physically at the line — so it is pushed too;
    the flag is a WMS supervisor matter.
  * "RM Dispatch", the two reversal rows, "RM Flag Cleared", feed rows (source
    "Push API"), uploads and Inventory Count rows never get a status from this
    module. Count scans have their own push (count_push); the two never overlap
    because they select on transaction_type.

Flow (mirrors count_push, which documents the claim/recover rules in detail):
  * rm_receive calls on_rm_received before its commit: sap_status = "Pending"
    (or "Disabled" while rm_push_enabled is OFF) and a sweep is enqueued.
  * sweep() atomically claims Pending rows (-> "Sending"), batches them, marks
    each Sent / Failed from SAP's per-row MESSAGE. Transport failure / 5xx
    releases the batch back to Pending (SAP de-dups on BATCH+ENTRYDATE+ENTRYTIME,
    so retries are safe). Rows stuck in "Sending" are recovered every sweep.
  * rm_remove (undo of the receive leg) calls on_rm_removed.

Every attempt is written to WMSLite SAP Log (direction Outbound, action
"RM Receipt"). rm_push_enabled is OFF by default; historic receipts made before
this shipped keep a NULL sap_status and are never pushed unless armed by hand.
"""

import json

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from sbx_wmslite.loading_push import (
	NO_SCAN_PLANT,
	_key,
	_log,
	_settings,
	parse_inventory_barcode,
	post_rows_resolving,
	row_outcome,
)

# Mirrors rm_api.TXN_RECEIPT — rm_api imports this module, so the literal is
# repeated here rather than imported (no cycle).
RM_TXN_TYPE = "RM Receipt"
LOG_ACTION = "RM Receipt"
SWEEP_JOB = "sbx_wmslite.rm_push.sweep"
REMOVED_NOTE = "Receipt undone in WMS after SAP send — reconcile in SAP"
# See count_push.STUCK_SECONDS.
STUCK_SECONDS = 600

PUSH_ROLES = ("Loading Supervisor", "System Manager")

# Columns push_batch needs from a claimed row. An RM ledger row carries no
# grade/weight/qc of its own (see rm_api._ledger), so those come from the coil's
# Bin Inventory row or the scanned QR.
_ROW_FIELDS = ["name", "coil_barcode", "coil_qr_raw", "rm_destination", "scanned_at"]


def _enabled(s=None):
	return bool(cint((s or _settings()).get("rm_push_enabled")))


def _enqueue_sweep():
	# One queued sweep at a time (see count_push._enqueue_sweep).
	frappe.enqueue(SWEEP_JOB, queue="short", enqueue_after_commit=True,
				   job_id=SWEEP_JOB, deduplicate=True)


def _is_rm_row(d):
	return bool(d) and d.transaction_type == RM_TXN_TYPE and d.source == "In-app"


def _collect_bins(codes):
	"""Bin Inventory attributes for the payload, including the QC hold flag —
	the count push reads QC off the scan itself, an RM receipt has to read it off
	the coil."""
	if not codes:
		return {}
	rows = frappe.get_all(
		"Bin Inventory", filters={"coil_barcode": ["in", list(set(codes))]},
		fields=["coil_barcode", "plant", "product", "sized", "weight", "material_grade", "qc"])
	return {r.coil_barcode: r for r in rows}


# --------------------------------------------------------------------------
# payload
# --------------------------------------------------------------------------
def build_rm_row(txn, bin_row, s):
	"""Assemble one SAP T2 transaction from a line receipt.

	`txn` is a Coil Transaction row (dict): coil_barcode, coil_qr_raw,
	rm_destination, scanned_at. Field sources, in priority order: the full
	scanned QR, then the coil's Bin Inventory row (fed by SAP). MATERIAL falls
	back to "NA" exactly like the Apps Script; PLANT to the NO_SCAN_PLANT
	sentinel. LOCATION is the destination line — the coil's location from the
	moment of receipt.
	"""
	p = parse_inventory_barcode(txn.get("coil_qr_raw")) if txn.get("coil_qr_raw") else {}
	b = bin_row or {}
	ts = get_datetime(txn.get("scanned_at")) if txn.get("scanned_at") else now_datetime()
	return {
		"TRANSACTIONTYPE": (s.get("rm_txn_type") or "T2").strip(),
		"PLANT": p.get("plant") or b.get("plant") or NO_SCAN_PLANT,
		"LOCATION": txn.get("rm_destination") or "",
		"BATCH": txn.get("coil_barcode"),
		"WEIGHT": p.get("weight") or b.get("weight") or "",
		"PRODUCT": p.get("product") or b.get("product") or "",
		"MATERIAL": p.get("material") or b.get("material_grade") or "NA",
		"SIZED": p.get("sized") or b.get("sized") or "",
		# SAP's feed carries the literal "QC" for a coil on hold, else blank.
		"QC": "QC" if cint(b.get("qc")) else "",
		"ENTRYDATE": ts.strftime("%d%m%Y"),
		"ENTRYTIME": ts.strftime("%H%M%S"),
		"USERNAME": s.get("loading_push_user") or "jindal1",
	}


# --------------------------------------------------------------------------
# hooks from rm_api
# --------------------------------------------------------------------------
def on_rm_received(txn_name):
	"""Arm a freshly inserted line receipt. Called inside rm_receive's transaction,
	before its commit; the sweep is enqueued after_commit so it can never see an
	uncommitted row. Never raises — a tracking fault must not fail the receipt."""
	try:
		if not _enabled():
			frappe.db.set_value("Coil Transaction", txn_name, "sap_status", "Disabled",
								update_modified=False)
			return {"status": "Disabled"}
		frappe.db.set_value("Coil Transaction", txn_name, "sap_status", "Pending",
							update_modified=False)
		_enqueue_sweep()
		return {"status": "Pending"}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "WMSLite rm-push arm failed")
		return {"status": "error"}


def on_rm_removed(txn_name):
	"""The operator undid this receipt (row is being marked reversed by rm_api).

	Not yet sent (Pending / Disabled / Failed / blank) -> "Not Required".
	Already Sent (or in flight) -> the status stands — SAP has the row and is not
	told — and a reconcile note is appended for the supervisor."""
	status, msg = frappe.db.get_value("Coil Transaction", txn_name,
									  ["sap_status", "sap_message"]) or (None, None)
	if status in ("Sent", "Sending"):
		note = f"{msg.strip()} | {REMOVED_NOTE}" if (msg or "").strip() else REMOVED_NOTE
		frappe.db.set_value("Coil Transaction", txn_name, "sap_message", note[:500],
							update_modified=False)
	else:
		frappe.db.set_value("Coil Transaction", txn_name, "sap_status", "Not Required",
							update_modified=False)


# --------------------------------------------------------------------------
# sweep: claim -> push -> record
# --------------------------------------------------------------------------
def _recover_stuck(s):
	"""Rows a dead worker left in "Sending" go back to Pending."""
	horizon = max(STUCK_SECONDS, 2 * (cint(s.sap_gi_timeout) or 30) + 60)
	cutoff = add_to_date(now_datetime(), seconds=-horizon)
	frappe.db.sql("""
		UPDATE `tabCoil Transaction`
		SET sap_status = 'Pending'
		WHERE sap_status = 'Sending'
		  AND transaction_type = %(tt)s AND source = 'In-app'
		  AND (sap_pushed_at IS NULL OR sap_pushed_at < %(cutoff)s)""",
		{"tt": RM_TXN_TYPE, "cutoff": cutoff})


def _claim(limit):
	"""Atomically move up to `limit` Pending receipts to "Sending" and return them
	(see count_push._claim for why the locking SELECT serialises sweeps)."""
	names = [r[0] for r in frappe.db.sql("""
		SELECT name FROM `tabCoil Transaction`
		WHERE transaction_type = %(tt)s AND source = 'In-app'
		  AND sap_status = 'Pending' AND reversed = 0
		ORDER BY scanned_at ASC, creation ASC
		LIMIT %(limit)s
		FOR UPDATE""", {"tt": RM_TXN_TYPE, "limit": cint(limit)})]
	if not names:
		frappe.db.commit()
		return []
	frappe.db.sql("""
		UPDATE `tabCoil Transaction`
		SET sap_status = 'Sending', sap_pushed_at = %(now)s
		WHERE name IN %(names)s AND sap_status = 'Pending' AND reversed = 0""",
		{"now": now_datetime(), "names": tuple(names)})
	frappe.db.commit()
	return frappe.get_all("Coil Transaction",
						  filters={"name": ["in", names], "sap_status": "Sending"},
						  fields=_ROW_FIELDS, order_by="scanned_at asc, creation asc")


def _release(names):
	"""Transport / 5xx: hand the claimed rows back to the next sweep."""
	if names:
		frappe.db.sql("""UPDATE `tabCoil Transaction` SET sap_status = 'Pending'
			WHERE name IN %(names)s AND sap_status = 'Sending'""", {"names": tuple(names)})


def push_batch(chunk, bins, s):
	"""Push one batch of claimed ("Sending") receipts and record the outcome."""
	payload, key_to_txn = [], {}
	for t in chunk:
		row = build_rm_row(t, bins.get(t.coil_barcode), s)
		payload.append(row)
		key_to_txn[_key(row)] = t.name
	names = [t.name for t in chunk]

	res = post_rows_resolving(payload, s)
	if res["kind"] in ("transport_error", "server_error"):
		_release(names)
		_log("Error", res["message"], count=len(payload), payload=json.dumps(payload),
			 action=LOG_ACTION)
		frappe.db.commit()
		return {"sent": 0, "failed": 0, "deferred": len(payload)}

	# An undo can land while the batch is in flight; honour it in the outcome.
	reversed_now = set(frappe.get_all("Coil Transaction",
									  filters={"name": ["in", names], "reversed": 1},
									  pluck="name"))
	now, sent, failed = now_datetime(), 0, 0
	for row in payload:
		name = key_to_txn[_key(row)]
		status, msg = row_outcome(row, res)
		if status == "Sent":
			sent += 1
		else:
			failed += 1
		if name in reversed_now:
			if status == "Sent":
				msg = f"{msg} | {REMOVED_NOTE}"
			else:
				status, msg = "Not Required", f"{msg} | undone in WMS before SAP accepted it"
		# Guarded by sap_status='Sending': a stuck-recovery that raced us and
		# re-armed the row must not be overwritten with a stale outcome.
		frappe.db.sql("""
			UPDATE `tabCoil Transaction`
			SET sap_status = %(st)s, sap_message = %(msg)s, sap_pushed_at = %(now)s,
				sap_attempts = COALESCE(sap_attempts, 0) + 1,
				sap_request = %(req)s, sap_response = %(resp)s
			WHERE name = %(name)s AND sap_status = 'Sending'""",
			{"st": status, "msg": msg[:500], "now": now,
			 "req": json.dumps(row, indent=2), "resp": res["text"][:4000], "name": name})

	_log("Accepted" if res["ok"] else "Rejected",
		 f"pushed {len(payload)} (sent {sent}, failed {failed}), HTTP {res['status_code']}",
		 count=len(payload), payload=json.dumps(payload), action=LOG_ACTION)
	frappe.db.commit()
	return {"sent": sent, "failed": failed, "deferred": 0}


def sweep(limit=2000):
	"""Scheduler / enqueued entry point: push every Pending line receipt."""
	s = _settings()
	if not _enabled(s):
		return {"skipped": "disabled"}
	_recover_stuck(s)
	rows = _claim(limit)
	if not rows:
		return {"pending": 0}
	bins = _collect_bins([r.coil_barcode for r in rows])
	batch = cint(s.loading_push_batch_size) or 200
	totals = {"sent": 0, "failed": 0, "deferred": 0, "pending": len(rows)}
	for i in range(0, len(rows), batch):
		res = push_batch(rows[i:i + batch], bins, s)
		for k in ("sent", "failed", "deferred"):
			totals[k] += res.get(k, 0)
	return totals


# --------------------------------------------------------------------------
# control: retry / push now / arm disabled
# --------------------------------------------------------------------------
def _row(txn_name):
	return frappe.db.get_value("Coil Transaction", txn_name,
							   ["name", "sap_status", "reversed", "transaction_type", "source"],
							   as_dict=True)


def _arm(txn_name):
	frappe.db.set_value("Coil Transaction", txn_name, "sap_status", "Pending",
						update_modified=False)
	_enqueue_sweep()


def requeue(txn_name):
	"""Console retry: re-arm a Failed receipt. Only Failed, only live rows."""
	if not _enabled():
		return {"skipped": "disabled"}
	d = _row(txn_name)
	if not _is_rm_row(d):
		return {"skipped": "not an RM receipt"}
	if cint(d.reversed):
		return {"skipped": "undone in WMS"}
	if d.sap_status != "Failed":
		return {"skipped": d.sap_status or "not pushed"}
	_arm(txn_name)
	return {"ok": True}


@frappe.whitelist()
def trigger_push(txn):
	"""Desk "Push Now" on a Coil Transaction. Sent (SAP has it), Sending (in
	flight), reversed (nothing to say) and non-receipt rows are refused."""
	frappe.only_for(PUSH_ROLES)
	if not _enabled():
		return {"skipped": "RM push disabled in WMSLite Settings"}
	d = _row(txn)
	if not _is_rm_row(d):
		return {"skipped": "not an RM line receipt"}
	if cint(d.reversed):
		return {"skipped": "undone in WMS — nothing to send"}
	if d.sap_status in ("Sent", "Sending"):
		return {"skipped": d.sap_status}
	_arm(txn)
	return {"ok": True}


@frappe.whitelist()
def push_disabled():
	"""Arm every live receipt parked at "Disabled" (received while the push was
	OFF) and sweep. Rows go out with their ORIGINAL ENTRYDATE/ENTRYTIME."""
	frappe.only_for(PUSH_ROLES)
	if not _enabled():
		return {"skipped": "RM push disabled in WMSLite Settings"}
	names = frappe.get_all("Coil Transaction",
						   filters={"transaction_type": RM_TXN_TYPE, "source": "In-app",
									"sap_status": "Disabled", "reversed": 0},
						   pluck="name")
	for n in names:
		frappe.db.set_value("Coil Transaction", n, "sap_status", "Pending", update_modified=False)
	if names:
		_enqueue_sweep()
	return {"armed": len(names)}
