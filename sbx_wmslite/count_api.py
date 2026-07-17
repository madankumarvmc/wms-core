"""Inventory Count (bin<->coil mapping capture) — HHT endpoints.

Despite the name, the MVP is a *mapping capture*, not a stock reconciliation:
an operator scans a bin, then scans each coil physically in that bin. Each coil
scan is recorded as an append-only **Coil Transaction** (source "In-app",
transaction_type "Inventory Count") and derived immediately into the single
**Bin Inventory** row for that coil — latest-scan-wins, exactly like the SAP
push feed. There is no variance / missing-coil logic in the MVP (Phase 2).

Reuses the intake pipeline that already exists for the SAP feed
(`bin_api.upsert_bin` / `process_pending_transactions`), so idempotency,
latest-wins and the scheduler safety-net are inherited unchanged. The only new
thing here is a producer of In-app transactions plus the role gate.

Every endpoint asserts the caller's role server-side — UI hiding is a
convenience, not a security boundary.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from sbx_wmslite.bin_api import upsert_bin
from sbx_wmslite.decode import decode_bin_code, decode_coil_id

LOADING_ROLES = {"Loading Operator", "Loading Supervisor", "System Manager"}
COUNT_ROLES = {"Inventory Recorder", "Loading Supervisor", "System Manager"}


# --------------------------------------------------------------------------
# guards
# --------------------------------------------------------------------------
def _require_count_role():
	if not (set(frappe.get_roles()) & COUNT_ROLES):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


def _count_enabled():
	# Read fresh (not the cached Single) — a cached Single can mask a
	# just-changed flag (see the outbound-SAP note in the app history).
	return bool(cint(frappe.db.get_single_value("WMSLite Settings", "count_enabled")))


def _require_count_enabled():
	if not _count_enabled():
		frappe.throw(_("Inventory Count is disabled"))


def _current_bin(coil):
	"""The bin the coil is currently mapped to (its Bin Inventory row), or None."""
	if not coil:
		return None
	return frappe.db.get_value("Bin Inventory", coil, "bin_code")


# --------------------------------------------------------------------------
# task selection
# --------------------------------------------------------------------------
@frappe.whitelist()
def my_tasks():
	"""Which HHT tasks the current user may perform (drives the task picker).

	`loading` needs a loading role; `count` needs a count role AND the feature
	flag on. Superusers (Supervisor / System Manager) get both.
	"""
	roles = set(frappe.get_roles())
	tasks = []
	if roles & LOADING_ROLES:
		tasks.append("loading")
	if (roles & COUNT_ROLES) and _count_enabled():
		tasks.append("count")
	return {"tasks": tasks, "default": None}


# --------------------------------------------------------------------------
# count flow
# --------------------------------------------------------------------------
@frappe.whitelist()
def count_open_bin(raw_bin):
	"""Decode a scanned bin and return the coils already mapped to it.

	Gives resume support: reopening a bin mid-count repopulates the list from
	Bin Inventory truth. `raw_bin` is the raw scan (decoded here, server-side).
	"""
	_require_count_role()
	_require_count_enabled()
	bin_code = decode_bin_code(raw_bin)
	if not bin_code:
		frappe.throw(_("Couldn't read the bin barcode"))
	rows = frappe.get_all(
		"Bin Inventory", filters={"bin_code": bin_code},
		fields=["coil_barcode", "material_grade", "weight", "qc", "scanned_at"],
		order_by="scanned_at desc", limit_page_length=0)
	coils = [{
		"coil_barcode": r.coil_barcode, "material_grade": r.material_grade,
		"weight": r.weight, "qc": cint(r.qc),
		"scanned_at": str(r.scanned_at) if r.scanned_at else None,
	} for r in rows]
	return {"ok": True, "bin_code": bin_code, "coils": coils,
			"count": len(coils), "qc_count": sum(c["qc"] for c in coils)}


@frappe.whitelist()
def count_scan(bin_code, raw_qr, client_event_id=None, qc=0):
	"""Record one coil as present in `bin_code`. Idempotent on client_event_id.

	Creates a Coil Transaction (In-app) and derives Bin Inventory immediately.
	The scan timestamp is the SERVER clock, never the device — device skew must
	not win the latest-scan-wins race. Returns whether the coil moved from
	another bin (`moved` + `previous_bin`) and whether it was already in this bin
	(`existing`).
	"""
	_require_count_role()
	_require_count_enabled()
	bin_code = (bin_code or "").strip()
	coil = decode_coil_id(raw_qr)
	if not bin_code:
		frappe.throw(_("Scan a bin before scanning coils"))
	if not coil:
		frappe.throw(_("Couldn't read the coil barcode"))
	qc = 1 if cint(qc) else 0

	# Idempotent replay (offline queue): same client_event_id => no double insert.
	if client_event_id and frappe.db.exists("Coil Transaction", {"txn_key": client_event_id}):
		prev = _current_bin(coil)
		return {"ok": True, "idempotent": True, "coil_id": coil, "previous_bin": prev,
				"moved": 0, "qc": qc, "existing": 1 if prev == bin_code else 0}

	previous_bin = _current_bin(coil)  # before this scan
	existing = 1 if (previous_bin and previous_bin == bin_code) else 0
	moved = 1 if (previous_bin and previous_bin != bin_code) else 0

	ts = now_datetime()
	txn = frappe.new_doc("Coil Transaction")
	txn.coil_barcode = coil
	txn.bin_code = bin_code
	txn.scanned_at = ts
	txn.transaction_type = "Inventory Count"
	txn.username = frappe.session.user
	txn.qc = qc
	txn.source = "In-app"
	txn.txn_key = client_event_id or f"count_{coil}_{ts.strftime('%Y%m%d%H%M%S%f')}"
	# Record the location the coil held *before* this scan so an undo can revert
	# precisely (rather than guessing) — see count_remove / _rederive_coil.
	txn.raw_json = json.dumps({"bin_code": bin_code, "raw_qr": raw_qr, "qc": qc,
							   "previous_bin": previous_bin})
	txn.insert(ignore_permissions=True)

	# Derive now: only the location fields are set, so grade/weight the SAP feed
	# supplied are never blanked (upsert_bin's non-empty-field rule).
	upsert_bin(coil, {"bin_code": bin_code, "scanned_at": ts, "qc": qc}, source="In-app")
	frappe.db.set_value("Coil Transaction", txn.name,
						{"processed": 1, "processed_at": ts,
						 "bin_inventory": coil if frappe.db.exists("Bin Inventory", coil) else None},
						update_modified=False)
	frappe.db.commit()
	return {"ok": True, "coil_id": coil, "previous_bin": previous_bin,
			"moved": moved, "qc": qc, "existing": existing}


@frappe.whitelist()
def count_set_qc(bin_code, coil_barcode, qc):
	"""Toggle the QC hold on a just-counted coil's Bin Inventory row."""
	_require_count_role()
	_require_count_enabled()
	coil_barcode = (coil_barcode or "").strip()
	qc = 1 if cint(qc) else 0
	if coil_barcode and frappe.db.exists("Bin Inventory", coil_barcode):
		frappe.db.set_value("Bin Inventory", coil_barcode, "qc", qc, update_modified=False)
		frappe.db.commit()
	return {"ok": True, "coil_barcode": coil_barcode, "qc": qc}


@frappe.whitelist()
def count_remove(bin_code, coil_barcode, client_event_id=None):
	"""Undo a mis-scan: drop the coil's In-app scan(s) in this bin and re-derive.

	Deletes the exact transaction (by client_event_id) when known, else this
	operator's In-app scans of the coil into this bin, then recomputes the coil's
	current location from whatever transactions remain (latest-wins). A coil with
	no remaining transactions is unmapped only if its Bin Inventory row was
	created in-app — SAP-fed rows are never deleted by a count undo.
	"""
	_require_count_role()
	_require_count_enabled()
	coil_barcode = (coil_barcode or "").strip()
	bin_code = (bin_code or "").strip()
	if not coil_barcode:
		return {"ok": True}
	if client_event_id:
		filters = {"txn_key": client_event_id}
	else:
		filters = {"coil_barcode": coil_barcode, "bin_code": bin_code, "source": "In-app"}
	rows = frappe.get_all("Coil Transaction", filters=filters,
						  fields=["name", "raw_json"], order_by="creation asc")
	# The location before the *earliest* removed scan — the true "before" state.
	fallback_bin = None
	if rows:
		try:
			fallback_bin = (json.loads(rows[0].raw_json or "{}") or {}).get("previous_bin")
		except (ValueError, TypeError):
			fallback_bin = None
	for r in rows:
		frappe.delete_doc("Coil Transaction", r.name, force=1, ignore_permissions=True)
	_rederive_coil(coil_barcode, fallback_bin)
	frappe.db.commit()
	return {"ok": True, "coil_barcode": coil_barcode, "current_bin": _current_bin(coil_barcode)}


def _rederive_coil(coil, fallback_bin=None):
	"""Recompute a coil's Bin Inventory row after transactions were removed.

	Order of truth: (1) the latest remaining In-app/other transaction, force-written
	(bypassing the latest-wins guard, which would refuse to move *back*);
	(2) else the `fallback_bin` captured from the removed scan (the coil's location
	before it) — this is how a coil fed by SAP is safely restored; (3) else the
	coil had no prior location at all (brand-new mis-scan) → drop the row."""
	latest = frappe.get_all(
		"Coil Transaction", filters={"coil_barcode": coil},
		fields=["bin_code", "qc", "scanned_at"],
		order_by="scanned_at desc, creation desc", limit_page_length=1)
	if latest:
		t = latest[0]
		if frappe.db.exists("Bin Inventory", coil):
			frappe.db.set_value("Bin Inventory", coil,
								{"bin_code": t.bin_code, "qc": cint(t.qc),
								 "scanned_at": t.scanned_at, "updated_at": now_datetime()},
								update_modified=False)
		else:
			upsert_bin(coil, {"bin_code": t.bin_code, "qc": cint(t.qc),
							  "scanned_at": t.scanned_at}, source="In-app")
		return
	if fallback_bin:
		if frappe.db.exists("Bin Inventory", coil):
			frappe.db.set_value("Bin Inventory", coil,
								{"bin_code": fallback_bin, "updated_at": now_datetime()},
								update_modified=False)
		else:
			upsert_bin(coil, {"bin_code": fallback_bin}, source="In-app")
		return
	# No transaction history and no prior location — a brand-new coil mis-scan.
	if frappe.db.exists("Bin Inventory", coil):
		frappe.delete_doc("Bin Inventory", coil, force=1, ignore_permissions=True)


@frappe.whitelist()
def count_finish(bin_code):
	"""'Save & next bin' — a confirmation/close, not a commit (scans persist per
	scan). No variance in the MVP. Kicks the derivation sweep in case a scan's
	immediate derivation was missed, and returns the bin's current coil count.
	"""
	_require_count_role()
	_require_count_enabled()
	frappe.enqueue("sbx_wmslite.bin_api.process_pending_transactions",
				   queue="short", enqueue_after_commit=True)
	bin_code = (bin_code or "").strip()
	total = frappe.db.count("Bin Inventory", {"bin_code": bin_code}) if bin_code else 0
	return {"ok": True, "bin_code": bin_code, "count": total}
