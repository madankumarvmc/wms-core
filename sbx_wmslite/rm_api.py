"""RM Movement (raw material: plant gate -> production line) — HHT endpoints.

A two-legged transfer with an in-transit state between the legs:

  Gate   `rm_dispatch`  scan the destination line, then each coil  -> In Transit to Line
  Line   `rm_receive`   scan against the expected list             -> Received

Receipt is STRICT: a coil that was not dispatched to this line is refused, live, at
the moment of the scan. The one exception is a replayed offline scan, which is
accepted and flagged instead — refusing it would destroy a receipt that really
happened, since the coil is physically at the line whatever the state now says.

Two flows the app already has, merged: the gate leg is the Inventory Count UX
(scan a location, then scan-and-go coils, one Save), the line leg is the Loading
UX (an expected list with per-coil progress).

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
**It never writes `Bin Inventory.bin_code`.** A coil in transit keeps its warehouse
bin, because the truck-loading flow reads that field: `api.get_plan` ->
`bin_api.resolve_plan_bins` stamps it onto the loading plan's coil rows, and the
handheld then *blocks* picking a coil whose bin doesn't match the one the operator
opened. Writing a production line into `bin_code` would send the truck picker to
the wrong place and then refuse the right one. Physical RM location is
`rm_destination` + `rm_status` instead.

Consequences of that choice, all deliberate:
  * `upsert_bin` is never called, so its latest-scan-wins guard cannot silently
    drop a receipt. (That guard compares against `scanned_at`, which the SAP feed
    fills from naive `ENTRYDATE`/`ENTRYTIME` with no timezone conversion — a feed
    posting IST to a UTC site sits 5h30m in the future and would have made every
    receipt a silent no-op for hours at a stretch.)
  * Ledger rows leave `bin_code` blank too, so nothing can derive a location from
    an RM row. `rm_destination` is its own indexed column for that reason.
  * `Bin Inventory.updated_at` is not bumped: that column orders the console's
    Inventory view ("what just moved"), and RM traffic would push real warehouse
    movement off the first page.

THE LEDGER IS THE DURABLE RECORD
--------------------------------
There is no header/trip document, so the line's expected list comes from coil
state. But `Bin Inventory` is a feed-owned row that `bin_api.receive_bin_inventory`
can hard-delete, which would make an in-transit coil vanish mid-transit. So
`rm_open_line` reconciles the fast path (Bin Inventory) against the ledger and
recovers anything missing, and rows are marked `reversed` rather than deleted.

OFFLINE ORDERING
----------------
Handhelds sync independently, so a gate device that was offline can replay a
dispatch *after* the line has already received the coil. Every state decision
therefore uses the device's `occurred_at` plus `synced_offline`, never the server
clock alone — see `rm_dispatch`'s supersede guard, which is the single most
important rule in this module.

Every endpoint asserts the caller's role server-side. UI hiding is a convenience,
not a security boundary.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from sbx_wmslite import tasks
from sbx_wmslite.bin_api import RM_IN_TRANSIT
from sbx_wmslite.decode import decode_coil_id
from sbx_wmslite.locations import resolve_location

# --- coil states -----------------------------------------------------------
# IN_TRANSIT is defined in bin_api (the lower-level module, which needs it to guard
# its remove branch) so there is exactly one definition of the string.
IN_TRANSIT = RM_IN_TRANSIT
RECEIVED = "Received"

# --- ledger transaction types ---------------------------------------------
TXN_DISPATCH = "RM Dispatch"
TXN_RECEIPT = "RM Receipt"
TXN_DISPATCH_REVERSAL = "RM Dispatch Reversal"
TXN_RECEIPT_REVERSAL = "RM Receipt Reversal"
# Written when a supervisor acknowledges a flagged receipt from the console. Kept in
# the ledger rather than done silently: clearing a flag is the moment somebody took
# responsibility for an exception, which is exactly what an audit needs to see.
TXN_FLAG_CLEARED = "RM Flag Cleared"

LEG_TYPES = {"dispatch": TXN_DISPATCH, "receive": TXN_RECEIPT}
REVERSAL_TYPES = {TXN_DISPATCH: TXN_DISPATCH_REVERSAL, TXN_RECEIPT: TXN_RECEIPT_REVERSAL}

# Re-exported from `tasks` so `rm_api.FINAL` still resolves for existing callers.
# It moved because `locations` needs it and rm_api imports locations.
FINAL = tasks.FINAL

# The RM state carried on Bin Inventory. Snapshotted verbatim into each ledger
# row's raw_json so an undo restores the exact prior state instead of guessing.
RM_STATE_FIELDS = (
	"rm_status", "rm_destination", "rm_source_bin", "rm_cycle",
	"rm_dispatched_by", "rm_dispatched_at", "rm_received_by", "rm_received_at",
	"rm_unplanned", "rm_expected_destination",
)

_BLANK_RM = {
	"rm_status": "", "rm_destination": "", "rm_source_bin": "", "rm_cycle": 0,
	"rm_dispatched_by": None, "rm_dispatched_at": None,
	"rm_received_by": None, "rm_received_at": None,
	"rm_unplanned": 0, "rm_expected_destination": "",
}


# --------------------------------------------------------------------------
# guards
# --------------------------------------------------------------------------
def _require_gate():
	tasks.require(tasks.RM_GATE_ROLES, "rm_enabled", _("RM Movement"))


def _require_line():
	tasks.require(tasks.RM_LINE_ROLES, "rm_enabled", _("RM Movement"))


def _final(msg):
	"""Throw a permanent failure — one an offline replay must give up on."""
	tasks.throw_final(msg)


def _settings_int(field, default):
	return cint(frappe.db.get_single_value("WMSLite Settings", field)) or default


# --------------------------------------------------------------------------
# state helpers
# --------------------------------------------------------------------------
def _state(coil):
	"""The coil's current RM state + warehouse bin, or None if it has no row."""
	if not coil:
		return None
	return frappe.db.get_value(
		"Bin Inventory", coil,
		["bin_code", "material_grade", "weight", "qc"] + list(RM_STATE_FIELDS),
		as_dict=True)


def _snapshot(prev):
	"""The pre-scan state, for an exact undo. Absent row -> the blank state."""
	if not prev:
		return dict(_BLANK_RM, bin_code=None)
	snap = {f: prev.get(f) for f in RM_STATE_FIELDS}
	snap["bin_code"] = prev.get("bin_code")
	return snap


def _apply_rm(coil, fields, prev):
	"""Write RM state onto the coil's Bin Inventory row. The ONLY writer of rm_*.

	Creates the row when the coil has never been seen (a brand-new coil first
	scanned at the gate). Two things make that necessary rather than optional:

	  * `frappe.db.set_value` on a missing row updates 0 rows and raises NOTHING —
	    the endpoint would return ok, the operator would see a green tick, and the
	    coil would never reach the line's expected list.
	  * `Bin Inventory.bin_code` is mandatory, so the row needs a placeholder. Its
	    `scanned_at` is left NULL on purpose: `upsert_bin`'s staleness guard only
	    fires when the stored row already has a timestamp, so a NULL guarantees the
	    first real feed or count scan overwrites the placeholder.

	`updated_at` is deliberately NOT bumped — see the module docstring.
	"""
	if prev is not None:
		frappe.db.set_value("Bin Inventory", coil, fields, update_modified=False)
		return 1

	doc = frappe.new_doc("Bin Inventory")
	doc.coil_barcode = coil
	doc.bin_code = frappe.db.get_single_value("WMSLite Settings", "rm_unknown_bin") or "UNKNOWN"
	doc.source = "In-app"
	for k, v in fields.items():
		setattr(doc, k, v)
	doc.insert(ignore_permissions=True)
	return 0


def _ledger(coil, txn_type, txn_key, destination, cycle, payload):
	"""Append one RM ledger row. Returns the doc, or None on a concurrent replay.

	`processed` is set BEFORE insert, not after (as count_scan does). The offline
	queue's exception handler does not roll back, so a row inserted and then
	orphaned at processed=0 would be committed by the *next* event in the batch and
	then picked up by the derive sweep — which reads `{"processed": 0}` with no type
	filter and would relabel it `source="Push API"`.
	"""
	ts = now_datetime()
	txn = frappe.new_doc("Coil Transaction")
	txn.coil_barcode = coil
	# The extracted code is the identity; the string it was extracted from is kept
	# beside it so a row can always be reconciled against the source system.
	txn.coil_qr_raw = (payload or {}).get("raw_qr") or ""
	# bin_code deliberately blank: an RM row is not a location statement.
	txn.scanned_at = ts
	txn.transaction_type = txn_type
	txn.rm_destination = destination
	txn.rm_cycle = cint(cycle)
	txn.username = frappe.session.user
	txn.source = "In-app"
	txn.txn_key = txn_key
	txn.processed = 1
	txn.processed_at = ts
	txn.bin_inventory = coil if frappe.db.exists("Bin Inventory", coil) else None
	txn.raw_json = json.dumps(payload, default=str)
	try:
		txn.insert(ignore_permissions=True)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		# Another replay of this same event won the race on the unique txn_key.
		# syncQueue fires from four different triggers with no in-flight lock, so
		# two batches really can carry the same event at once — the exists() check
		# above is not a lock. A unique *field* clash raises UniqueValidationError
		# (DuplicateEntryError is only for a docname clash), hence both.
		return None
	return txn


def _existing_txn(txn_key):
	if not txn_key:
		return None
	return frappe.db.get_value("Coil Transaction", {"txn_key": txn_key},
							   ["name", "transaction_type", "reversed"], as_dict=True)


def _coil_view(coil, st):
	"""The shape the handheld and console render a coil in."""
	return {
		"coil_barcode": coil,
		"material_grade": (st or {}).get("material_grade"),
		"weight": (st or {}).get("weight"),
		"qc": cint((st or {}).get("qc")),
		"rm_status": (st or {}).get("rm_status") or "",
		"rm_destination": (st or {}).get("rm_destination") or "",
		"rm_source_bin": (st or {}).get("rm_source_bin") or "",
		"rm_cycle": cint((st or {}).get("rm_cycle")),
		"rm_unplanned": cint((st or {}).get("rm_unplanned")),
		"rm_expected_destination": (st or {}).get("rm_expected_destination") or "",
		"rm_dispatched_at": str(st["rm_dispatched_at"]) if (st or {}).get("rm_dispatched_at") else None,
		"rm_received_at": str(st["rm_received_at"]) if (st or {}).get("rm_received_at") else None,
	}


def _decode_location(raw, label):
	"""Decode AND validate a scanned location — see `locations.resolve_location`.

	Both legs go through this. Before it validated, a coil QR scanned into "Scan the
	destination line" became a destination, and the coils sent there were invisible
	to the line that was actually waiting for them.
	"""
	return resolve_location(raw, label)


def _decode_coil(raw):
	coil = decode_coil_id(raw)
	if not coil:
		_final(_("Couldn't read the coil barcode"))
	return coil


# --------------------------------------------------------------------------
# gate leg — dispatch
# --------------------------------------------------------------------------
@frappe.whitelist()
def rm_open_destination(raw_location):
	"""Decode a scanned destination and list what is already in transit to it.

	Gives the gate operator resume support (reopening a destination mid-batch
	repopulates the list) and in-session dedupe.
	"""
	_require_gate()
	destination = _decode_location(raw_location, _("destination"))
	rows = frappe.get_all(
		"Bin Inventory",
		filters={"rm_destination": destination, "rm_status": IN_TRANSIT},
		fields=["coil_barcode", "material_grade", "weight", "qc", "rm_source_bin",
				"rm_cycle", "rm_dispatched_at"],
		order_by="rm_dispatched_at desc", limit_page_length=0)
	coils = [{
		"coil_barcode": r.coil_barcode, "material_grade": r.material_grade,
		"weight": r.weight, "qc": cint(r.qc), "rm_source_bin": r.rm_source_bin,
		"rm_cycle": cint(r.rm_cycle), "rm_status": IN_TRANSIT,
		"rm_dispatched_at": str(r.rm_dispatched_at) if r.rm_dispatched_at else None,
	} for r in rows]
	return {"ok": True, "destination": destination, "coils": coils, "count": len(coils)}


@frappe.whitelist()
def rm_dispatch(destination, raw_qr, client_event_id=None, occurred_at=None,
				synced_offline=0, confirm_reroute=0):
	"""Send one coil to `destination`. Idempotent on `client_event_id`.

	The scan timestamp written to the ledger is the SERVER clock; `occurred_at` (the
	device clock) is used only to order this scan against stored state, because a
	replayed event reaches the server long after it happened.

	Sending a coil that is already in transit somewhere else needs
	`confirm_reroute` — see the branch below.
	"""
	_require_gate()
	destination = (destination or "").strip()
	if not destination:
		_final(_("Scan a destination before scanning coils"))
	coil = _decode_coil(raw_qr)
	raw_scan = (raw_qr or "").strip()
	offline = 1 if cint(synced_offline) else 0
	occurred = get_datetime(occurred_at) if occurred_at else now_datetime()

	dup = _existing_txn(client_event_id)
	if dup:
		st = _state(coil)
		return {"ok": True, "idempotent": True, "coil_id": coil, **_coil_view(coil, st)}

	prev = _state(coil)
	cur_status = (prev or {}).get("rm_status") or ""
	cur_dest = (prev or {}).get("rm_destination") or ""
	cycle = cint((prev or {}).get("rm_cycle"))
	ts = now_datetime()
	user = frappe.session.user
	snapshot = _snapshot(prev)

	# ---- the supersede guard -------------------------------------------------
	# A replayed dispatch must never drag a coil back out of Received. Gate device
	# offline, line device online: the line receives the coil, then this dispatch
	# replays and would resurrect it onto the expected list forever.
	#
	# Deliberately NOT scoped to a matching destination: if the gate sent it to L1
	# and it was received at L2, a destination-scoped test would permit exactly the
	# worst version of this bug. And deliberately conditional on `synced_offline` —
	# a *live* dispatch of a previously-received coil is the legitimate start of its
	# next trip, so a blanket "never regress" rule would block every coil's second
	# movement for good.
	received_at = (prev or {}).get("rm_received_at")
	if (offline and cur_status == RECEIVED and received_at
			and get_datetime(received_at) >= occurred):
		payload = {"leg": "dispatch", "destination": destination, "raw_qr": raw_qr,
				   "occurred_at": occurred, "synced_offline": offline,
				   "superseded": 1, "bin_row_existed": 1, "prev": snapshot}
		_ledger(coil, TXN_DISPATCH, client_event_id or f"rmd_{coil}_{ts:%Y%m%d%H%M%S%f}",
				destination, cycle, payload)
		if cur_dest != destination:
			# Make the mismatch visible rather than swallowing it.
			_apply_rm(coil, {"rm_expected_destination": destination}, prev)
		frappe.db.commit()
		st = _state(coil)
		return {"ok": True, "superseded": True, "coil_id": coil,
				"message": _("Already received at {0}").format(cur_dest or "-"),
				**_coil_view(coil, st)}

	# ---- normal transitions --------------------------------------------------
	extra = {}
	if cur_status == IN_TRANSIT and cur_dest == destination:
		# Already dispatched here — refresh who/when, no new movement.
		fields = {"rm_dispatched_by": user, "rm_dispatched_at": ts}
		extra = {"already_in_transit": 1}
	elif cur_status == IN_TRANSIT and cur_dest and cur_dest != destination:
		# Re-routing: the coil is already on its way to another line. Taking it
		# silently would remove it from that line's expected list with nothing said
		# to anybody — the receiver simply waits for a coil that is never coming.
		# So a LIVE scan has to be confirmed. Nothing is written until it is.
		#
		# A replay is exempt: it is a scan that already happened on a device that
		# was offline, and there is nobody left to ask.
		if not offline and not cint(confirm_reroute):
			return {"ok": True, "needs_confirm": True, "coil_id": coil,
					"current_destination": cur_dest, "destination": destination,
					"message": _("Coil {0} is already on its way to {1}")
					.format(coil, cur_dest),
					**_coil_view(coil, prev)}
		# Same movement, redirected: keep the cycle and the source bin.
		fields = {"rm_destination": destination, "rm_dispatched_by": user,
				  "rm_dispatched_at": ts}
		extra = {"rerouted_from": cur_dest}
	else:
		# A new movement: blank state, or a coil starting its next trip.
		# The cycle is written as an absolute value (prev + 1), not an increment, so
		# that a concurrent double-apply cannot double-bump it.
		cycle = cycle + 1
		fields = dict(
			_BLANK_RM,
			rm_status=IN_TRANSIT,
			rm_destination=destination,
			rm_source_bin=(prev or {}).get("bin_code") or "",
			rm_cycle=cycle,
			rm_dispatched_by=user,
			rm_dispatched_at=ts,
		)
		extra = {"source_bin": fields["rm_source_bin"]}

	fields["coil_qr_raw"] = raw_scan
	existed = _apply_rm(coil, fields, prev)
	payload = {"leg": "dispatch", "destination": destination, "raw_qr": raw_qr,
			   "occurred_at": occurred, "synced_offline": offline,
			   "bin_row_existed": existed, "prev": snapshot}
	txn = _ledger(coil, TXN_DISPATCH,
				  client_event_id or f"rmd_{coil}_{ts:%Y%m%d%H%M%S%f}",
				  destination, cycle, payload)
	if txn is None:
		frappe.db.commit()
		return {"ok": True, "idempotent": True, "coil_id": coil,
				**_coil_view(coil, _state(coil))}
	frappe.db.commit()
	return {"ok": True, "coil_id": coil, **extra, **_coil_view(coil, _state(coil))}


# --------------------------------------------------------------------------
# line leg — receipt
# --------------------------------------------------------------------------
@frappe.whitelist()
def rm_open_line(raw_location):
	"""What the receiver at `raw_location` is supposed to receive, plus recent history.

	`expected` is the fast path (Bin Inventory) reconciled against the ledger:
	`bin_api.receive_bin_inventory` can hard-delete a Bin Inventory row, which would
	otherwise make an in-transit coil silently disappear from this list. Anything
	recovered from the ledger alone is flagged `recovered` so the console can show
	that its index row went missing.
	"""
	_require_line()
	line = _decode_location(raw_location, _("line"))
	hours = _settings_int("rm_received_history_hours", 12)

	rows = frappe.get_all(
		"Bin Inventory",
		filters={"rm_destination": line, "rm_status": IN_TRANSIT},
		fields=["coil_barcode", "material_grade", "weight", "qc", "rm_source_bin",
				"rm_cycle", "rm_dispatched_at", "rm_dispatched_by"],
		order_by="rm_dispatched_at asc", limit_page_length=0)
	expected = [{
		"coil_barcode": r.coil_barcode, "material_grade": r.material_grade,
		"weight": r.weight, "qc": cint(r.qc), "rm_source_bin": r.rm_source_bin,
		"rm_cycle": cint(r.rm_cycle), "recovered": 0,
		"rm_dispatched_at": str(r.rm_dispatched_at) if r.rm_dispatched_at else None,
		"rm_dispatched_by": r.rm_dispatched_by,
	} for r in rows]
	seen = {c["coil_barcode"] for c in expected}

	# Ledger reconciliation: dispatched here, never received for that cycle, and
	# not already covered by the fast path above.
	orphans = frappe.db.sql("""
		SELECT d.coil_barcode, d.rm_cycle, d.scanned_at, d.username
		FROM `tabCoil Transaction` d
		WHERE d.transaction_type = %(dispatch)s
		  AND d.rm_destination = %(line)s
		  AND d.reversed = 0
		  AND NOT EXISTS (
			SELECT 1 FROM `tabCoil Transaction` r
			WHERE r.transaction_type = %(receipt)s
			  AND r.reversed = 0
			  AND r.coil_barcode = d.coil_barcode
			  AND r.rm_cycle = d.rm_cycle)
		ORDER BY d.scanned_at ASC""",
		{"dispatch": TXN_DISPATCH, "receipt": TXN_RECEIPT, "line": line}, as_dict=True)
	for o in orphans:
		if o.coil_barcode in seen:
			continue
		st = _state(o.coil_barcode)
		# Only genuinely missing rows are recovered. The fast path above is the truth
		# about where a coil is going NOW; this branch exists solely for coils whose
		# Bin Inventory row went missing under it. So anything the fast path
		# deliberately left out stays out:
		#   * already Received;
		#   * on a later trip than the dispatch we are looking at;
		#   * re-routed — it is in transit to a DIFFERENT line now, and resurrecting
		#     it here would put it back on the list of the line it was taken off,
		#     which is exactly the confusion the re-route confirmation exists to
		#     prevent. (The destination test was described in this comment before it
		#     was implemented; the code only checked the first two.)
		if st and ((st.get("rm_status") or "") == RECEIVED
				   or cint(st.get("rm_cycle")) != cint(o.rm_cycle)
				   or (st.get("rm_destination") or "") != line):
			continue
		seen.add(o.coil_barcode)
		expected.append({
			"coil_barcode": o.coil_barcode, "material_grade": (st or {}).get("material_grade"),
			"weight": (st or {}).get("weight"), "qc": cint((st or {}).get("qc")),
			"rm_source_bin": (st or {}).get("rm_source_bin") or "",
			"rm_cycle": cint(o.rm_cycle), "recovered": 1,
			"rm_dispatched_at": str(o.scanned_at) if o.scanned_at else None,
			"rm_dispatched_by": o.username,
		})

	since = add_to_date(now_datetime(), hours=-hours)
	recent = frappe.get_all(
		"Bin Inventory",
		filters={"rm_destination": line, "rm_status": RECEIVED,
				 "rm_received_at": (">=", since)},
		fields=["coil_barcode", "material_grade", "weight", "rm_unplanned",
				"rm_received_at", "rm_received_by", "rm_expected_destination"],
		order_by="rm_received_at desc", limit_page_length=0)
	received_recent = [{
		"coil_barcode": r.coil_barcode, "material_grade": r.material_grade,
		"weight": r.weight, "rm_unplanned": cint(r.rm_unplanned),
		"rm_expected_destination": r.rm_expected_destination or "",
		"rm_received_at": str(r.rm_received_at) if r.rm_received_at else None,
		"rm_received_by": r.rm_received_by,
	} for r in recent]

	return {"ok": True, "destination": line, "expected": expected,
			"received_recent": received_recent, "expected_count": len(expected),
			"received_count": len(received_recent), "history_hours": hours,
			"recovered_count": sum(c["recovered"] for c in expected)}


@frappe.whitelist()
def rm_receive(destination, raw_qr, client_event_id=None, occurred_at=None,
			   synced_offline=0):
	"""Receive one coil at `destination`. Idempotent on `client_event_id`.

	A LIVE scan of a coil that is not in transit to this line is REFUSED: nothing is
	written and the operator is told why. A REPLAYED scan (`synced_offline`) is still
	accepted and flagged `rm_unplanned`, because the device captured it at a moment
	when the coil legitimately was on its expected list — see the branch comment.
	"""
	_require_line()
	destination = (destination or "").strip()
	if not destination:
		_final(_("Scan your line before scanning coils"))
	coil = _decode_coil(raw_qr)
	raw_scan = (raw_qr or "").strip()
	offline = 1 if cint(synced_offline) else 0
	occurred = get_datetime(occurred_at) if occurred_at else now_datetime()

	if _existing_txn(client_event_id):
		return {"ok": True, "idempotent": True, "coil_id": coil,
				**_coil_view(coil, _state(coil))}

	prev = _state(coil)
	cur_status = (prev or {}).get("rm_status") or ""
	cur_dest = (prev or {}).get("rm_destination") or ""
	cycle = cint((prev or {}).get("rm_cycle"))
	ts = now_datetime()
	user = frappe.session.user
	snapshot = _snapshot(prev)

	# Already received here: two receivers can both be looking at the same expected
	# list. Return without writing a second ledger row — a duplicate row's snapshot
	# would record "Received" as the prior state and corrupt a later undo.
	if cur_status == RECEIVED and cur_dest == destination:
		return {"ok": True, "already_received": True, "coil_id": coil,
				**_coil_view(coil, prev)}

	extra = {}
	unplanned = 0
	expected_dest = ""
	if cur_status == IN_TRANSIT and cur_dest == destination:
		pass  # the happy path
	elif not offline:
		# ---- the strict-receipt rule -------------------------------------------
		# A coil that is not in transit to this line cannot be received here. The
		# scan is refused outright: no ledger row, no state change, nothing to
		# unpick later. The operator is told which line it was actually sent to,
		# because that is the one fact that resolves the situation on the floor.
		#
		# This applies to LIVE scans only. A replayed scan falls through to the
		# branches below and is still accepted-and-flagged, and that asymmetry is
		# the whole point: the line device captured the scan at a moment when its
		# expected list said the coil belonged here, and the state only moved on
		# afterwards. Refusing it at replay time would destroy a receipt that
		# genuinely happened — the coil is physically at the line either way — and
		# leave the operator no way to re-enter it. Flagging surfaces it in the
		# console instead, where a supervisor can see it and clear it.
		if cur_status == IN_TRANSIT and cur_dest:
			_final(_("Coil {0} was sent to {1}, not to this line").format(coil, cur_dest))
		if cur_status == RECEIVED and cur_dest:
			_final(_("Coil {0} was already received at {1}").format(coil, cur_dest))
		_final(_("Coil {0} was not sent to this line").format(coil))
	elif cur_status == IN_TRANSIT and cur_dest and cur_dest != destination:
		# Mis-routed, or the gate re-dispatched it while this device was offline.
		# Either way a human scanned the coil HERE, which is the strongest evidence
		# available — receive it, and record where it was supposed to go.
		unplanned, expected_dest = 1, cur_dest
		dispatched_at = (prev or {}).get("rm_dispatched_at")
		if offline and dispatched_at and get_datetime(dispatched_at) > occurred:
			extra = {"conflict": 1, "reason": "redispatched"}
		else:
			extra = {"reason": "misrouted"}
	else:
		# Never dispatched, or already Received somewhere else.
		unplanned = 1
		expected_dest = cur_dest if cur_status == RECEIVED else ""
		extra = {"reason": "not_dispatched"}
		if not cycle:
			cycle = 1

	fields = {
		"rm_status": RECEIVED,
		"rm_destination": destination,
		"rm_cycle": cycle,
		"rm_received_by": user,
		"rm_received_at": ts,
		"rm_unplanned": unplanned,
		"rm_expected_destination": expected_dest,
	}
	fields["coil_qr_raw"] = raw_scan
	existed = _apply_rm(coil, fields, prev)
	payload = {"leg": "receipt", "destination": destination, "raw_qr": raw_qr,
			   "occurred_at": occurred, "synced_offline": offline,
			   "bin_row_existed": existed, "prev": snapshot}
	txn = _ledger(coil, TXN_RECEIPT,
				  client_event_id or f"rmr_{coil}_{ts:%Y%m%d%H%M%S%f}",
				  destination, cycle, payload)
	if txn is None:
		frappe.db.commit()
		return {"ok": True, "idempotent": True, "coil_id": coil,
				**_coil_view(coil, _state(coil))}
	frappe.db.commit()
	out = {"ok": True, "coil_id": coil, **extra, **_coil_view(coil, _state(coil))}
	if unplanned:
		out["unplanned"] = 1
	return out


# --------------------------------------------------------------------------
# undo
# --------------------------------------------------------------------------
@frappe.whitelist()
def rm_remove(coil_barcode, leg, client_event_id=None):
	"""Reverse a mis-scan on either leg, restoring the exact pre-scan state.

	Rows are marked `reversed`, never deleted. Deleting would free the unique
	`txn_key` and let a still-queued replay of the same event re-insert a scan the
	operator explicitly undid — and the reversed row is also what a later undo reads
	to reconstruct history (see the dispatch-already-reversed case below).
	"""
	_require_line() if leg == "receive" else _require_gate()
	coil_barcode = (coil_barcode or "").strip()
	txn_type = LEG_TYPES.get(leg)
	if not coil_barcode or not txn_type:
		_final(_("Nothing to undo"))

	if client_event_id:
		row = frappe.db.get_value(
			"Coil Transaction",
			{"txn_key": client_event_id, "transaction_type": txn_type, "reversed": 0},
			["name", "rm_cycle", "rm_destination", "raw_json"], as_dict=True)
	else:
		rows = frappe.get_all(
			"Coil Transaction",
			filters={"coil_barcode": coil_barcode, "transaction_type": txn_type,
					 "reversed": 0},
			fields=["name", "rm_cycle", "rm_destination", "raw_json"],
			order_by="creation desc", limit_page_length=1)
		row = rows[0] if rows else None
	if not row:
		_final(_("That scan is not on record — it may already be undone"))

	st = _state(coil_barcode)
	cur_status = (st or {}).get("rm_status") or ""

	# Refuse when the state has moved past the leg being undone. This also covers
	# the cross-device case: a gate device replaying [dispatch, remove] after the
	# line has already received the coil must not strip its Received state.
	if leg == "dispatch" and cur_status == RECEIVED:
		_final(_("Coil {0} is already received — the dispatch can no longer be undone")
			   .format(coil_barcode))
	if leg == "receive":
		newer = frappe.db.count("Coil Transaction", {
			"coil_barcode": coil_barcode, "reversed": 0,
			"transaction_type": ("in", (TXN_DISPATCH, TXN_RECEIPT)),
			"creation": (">", frappe.db.get_value("Coil Transaction", row.name, "creation")),
		})
		if newer:
			_final(_("A newer RM scan exists on coil {0} — this undo is no longer valid")
				   .format(coil_barcode))

	try:
		payload = json.loads(row.raw_json or "{}") or {}
	except (ValueError, TypeError):
		payload = {}
	restore = dict(payload.get("prev") or {})
	bin_row_existed = cint(payload.get("bin_row_existed", 1))

	# Undoing a receipt whose dispatch was ALSO reversed: restoring "In Transit"
	# verbatim would resurrect an in-transit coil with no live dispatch behind it —
	# it would sit on the line's expected list forever with nothing explaining why.
	if leg == "receive" and (restore.get("rm_status") or "") == IN_TRANSIT:
		if not _dispatch_is_live(coil_barcode, cint(restore.get("rm_cycle"))):
			restore = _fallback_before_dispatch(coil_barcode, cint(restore.get("rm_cycle")),
												restore)

	target = {f: restore.get(f, _BLANK_RM[f]) for f in RM_STATE_FIELDS}
	if st is not None:
		frappe.db.set_value("Bin Inventory", coil_barcode, target, update_modified=False)

	# A row this module invented for a coil nobody had ever seen is removed again —
	# but only if it is still the untouched placeholder and nothing else refers to it.
	if not bin_row_existed and st is not None:
		_drop_placeholder_row(coil_barcode, row.name)

	now = now_datetime()
	frappe.db.set_value("Coil Transaction", row.name,
						{"reversed": 1, "reversed_by": frappe.session.user,
						 "reversed_at": now}, update_modified=False)
	_ledger(coil_barcode, REVERSAL_TYPES[txn_type],
			f"rmx_{row.name}", row.rm_destination, cint(row.rm_cycle),
			{"leg": leg, "reverses": row.name, "restored": target})
	frappe.db.commit()
	return {"ok": True, "coil_id": coil_barcode, "reversed": row.name,
			**_coil_view(coil_barcode, _state(coil_barcode))}


def _dispatch_is_live(coil, cycle):
	"""Is there still an un-reversed dispatch behind this cycle?"""
	return bool(frappe.db.exists("Coil Transaction", {
		"coil_barcode": coil, "transaction_type": TXN_DISPATCH,
		"rm_cycle": cycle, "reversed": 0}))


def _fallback_before_dispatch(coil, cycle, current_restore):
	"""The state the coil held BEFORE the (now reversed) dispatch of `cycle`.

	Readable precisely because reversed rows are kept rather than deleted. If even
	that is gone, fall back to no RM state at all rather than inventing one.
	"""
	row = frappe.db.get_value(
		"Coil Transaction",
		{"coil_barcode": coil, "transaction_type": TXN_DISPATCH, "rm_cycle": cycle},
		"raw_json")
	if row:
		try:
			prev = (json.loads(row) or {}).get("prev") or {}
			if prev:
				return dict(prev)
		except (ValueError, TypeError):
			pass
	blank = dict(_BLANK_RM)
	blank["rm_source_bin"] = current_restore.get("rm_source_bin") or ""
	return blank


def _drop_placeholder_row(coil, undone_txn):
	"""Remove a Bin Inventory row this module created, if it is still untouched."""
	st = frappe.db.get_value("Bin Inventory", coil, ["bin_code", "source"], as_dict=True)
	if not st or st.source != "In-app":
		return
	placeholder = frappe.db.get_single_value("WMSLite Settings", "rm_unknown_bin") or "UNKNOWN"
	if (st.bin_code or "") != placeholder:
		return
	others = frappe.db.count("Coil Transaction", {
		"coil_barcode": coil, "reversed": 0, "name": ("!=", undone_txn)})
	if others:
		return
	frappe.delete_doc("Bin Inventory", coil, force=1, ignore_permissions=True)


# --------------------------------------------------------------------------
# save / close
# --------------------------------------------------------------------------
@frappe.whitelist()
def rm_finish(destination, leg="dispatch"):
	""""Save" — a confirmation, not a commit: every scan already persisted.

	Deliberately does NOT enqueue `process_pending_transactions` (as count_finish
	does): RM rows are marked processed before insert so they never need the sweep,
	and kicking it here would process unrelated stuck rows under this operator's
	request.
	"""
	_require_line() if leg == "receive" else _require_gate()
	destination = (destination or "").strip()
	if not destination:
		return {"ok": True, "destination": destination, "in_transit": 0, "received": 0}
	return {
		"ok": True,
		"destination": destination,
		"in_transit": frappe.db.count("Bin Inventory", {
			"rm_destination": destination, "rm_status": IN_TRANSIT}),
		"received": frappe.db.count("Bin Inventory", {
			"rm_destination": destination, "rm_status": RECEIVED}),
	}


# --------------------------------------------------------------------------
# supervisor actions (called from the console, not the handheld)
# --------------------------------------------------------------------------
def clear_unplanned_flag(coil_barcode, note=None):
	"""Acknowledge a flagged receipt. Caller MUST have checked console permission.

	Not whitelisted: it is reached through `console_api.rm_clear_flag`, which does
	the role check. Exposing it here would let an RM Line Operator clear their own
	exceptions, which is the one thing the flag exists to prevent.

	The flag is only dropped — `rm_status`/`rm_destination` are left exactly as they
	are. The coil is physically at the line that scanned it; clearing the flag says
	"a supervisor has seen this", not "this never happened".
	"""
	coil_barcode = (coil_barcode or "").strip()
	st = _state(coil_barcode)
	if not st:
		frappe.throw(_("Coil {0} has no inventory record").format(coil_barcode))
	if not cint(st.get("rm_unplanned")):
		return {"ok": True, "already_clear": True, "coil_id": coil_barcode,
				**_coil_view(coil_barcode, st)}

	was = st.get("rm_expected_destination") or ""
	frappe.db.set_value("Bin Inventory", coil_barcode,
						{"rm_unplanned": 0, "rm_expected_destination": ""},
						update_modified=False)
	_ledger(coil_barcode, TXN_FLAG_CLEARED,
			f"rmf_{coil_barcode}_{now_datetime():%Y%m%d%H%M%S%f}",
			st.get("rm_destination") or "", cint(st.get("rm_cycle")),
			{"leg": "flag_cleared", "was_expected_at": was,
			 "received_at_": str(st.get("rm_received_at") or ""), "note": note or ""})
	frappe.db.commit()
	return {"ok": True, "coil_id": coil_barcode, "was_expected_at": was,
			**_coil_view(coil_barcode, _state(coil_barcode))}
