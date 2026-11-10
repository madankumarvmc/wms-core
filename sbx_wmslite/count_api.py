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
from frappe.utils import cint, now_datetime, today

from sbx_wmslite import count_push, tasks
from sbx_wmslite.bin_api import RM_TXN_TYPES, upsert_bin
from sbx_wmslite.decode import decode_coil_id
from sbx_wmslite.locations import resolve_location

# Re-exported for callers that imported them from here before the registry existed.
LOADING_ROLES = tasks.LOADING_ROLES
COUNT_ROLES = tasks.COUNT_ROLES

# The type this module writes. count_remove only ever undoes its own scans, so it
# whitelists rather than excluding — In-app rows always carry a type.
COUNT_TXN_TYPE = "Inventory Count"


# --------------------------------------------------------------------------
# guards
# --------------------------------------------------------------------------
def _require_count_role():
	tasks.require(tasks.COUNT_ROLES, "count_enabled", _("Inventory Count"))


def _count_enabled():
	return tasks.flag_on("count_enabled")


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

	The logic lives in `sbx_wmslite.tasks` — one table covering Loading, Inventory
	Count and both RM Movement legs. This endpoint stays here because a handheld
	running a cached app shell calls this exact path.
	"""
	return tasks.my_tasks()


# --------------------------------------------------------------------------
# count flow
# --------------------------------------------------------------------------
@frappe.whitelist()
def count_open_bin(raw_bin):
	"""Decode a scanned bin and return this operator's own count scans in it today.

	Blind count: coils merely *mapped* to the bin (SAP-fed Bin Inventory rows or
	other operators' scans) are never returned, so the operator sees nothing until
	they scan. What does come back is resume support — reopening a bin after an
	app reload repopulates the in-progress list from the current user's live
	(non-reversed) In-app Inventory Count transactions in this bin since server
	midnight. Each coil carries `cid` (the txn_key) so a later ✕ reverses the exact
	scan. `raw_bin` is the raw scan (decoded and validated here, server-side).
	"""
	_require_count_role()
	# Validated, not merely decoded: a mistyped bin used to open an empty count
	# silently, which reads exactly like a bin that is genuinely empty.
	bin_code = resolve_location(raw_bin, _("bin"))
	rows = frappe.get_all(
		"Coil Transaction",
		filters={"transaction_type": COUNT_TXN_TYPE, "source": "In-app",
				 "bin_code": bin_code, "username": frappe.session.user,
				 "reversed": 0, "scanned_at": (">=", f"{today()} 00:00:00")},
		fields=["coil_barcode", "qc", "txn_key", "scanned_at"],
		order_by="scanned_at desc, creation desc", limit_page_length=0)
	# Latest scan per coil wins (a coil re-scanned in the same bin has two rows).
	seen, scans = set(), []
	for r in rows:
		if r.coil_barcode in seen:
			continue
		seen.add(r.coil_barcode)
		scans.append(r)
	inv = {}
	if scans:
		inv = {b.name: b for b in frappe.get_all(
			"Bin Inventory", filters={"name": ("in", [r.coil_barcode for r in scans])},
			fields=["name", "material_grade", "weight", "qc"], limit_page_length=0)}
	coils = []
	for r in scans:
		b = inv.get(r.coil_barcode)
		coils.append({
			"coil_barcode": r.coil_barcode,
			"material_grade": b.material_grade if b else None,
			"weight": b.weight if b else None,
			# QC toggles land on Bin Inventory, not on the scan transaction.
			"qc": cint(b.qc) if b else cint(r.qc),
			"scanned_at": str(r.scanned_at) if r.scanned_at else None,
			"cid": r.txn_key,
		})
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
	bin_code = (bin_code or "").strip()
	coil = decode_coil_id(raw_qr)
	raw_scan = (raw_qr or "").strip()
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
	# The extracted code is the identity; the full scan is kept for reconciliation.
	txn.coil_qr_raw = raw_scan
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
	upsert_bin(coil, {"bin_code": bin_code, "scanned_at": ts, "qc": qc,
					  "coil_qr_raw": raw_scan}, source="In-app")
	frappe.db.set_value("Coil Transaction", txn.name,
						{"processed": 1, "processed_at": ts,
						 "bin_inventory": coil if frappe.db.exists("Bin Inventory", coil) else None},
						update_modified=False)
	# Queue the SAP T1 push for this scan (Pending, or Disabled while the push is
	# off). Inside this transaction so the sweep — enqueued after_commit — can
	# never see an uncommitted row; it swallows its own errors.
	count_push.on_count_scanned(txn.name)
	frappe.db.commit()
	return {"ok": True, "coil_id": coil, "previous_bin": previous_bin,
			"moved": moved, "qc": qc, "existing": existing}


@frappe.whitelist()
def count_set_qc(bin_code, coil_barcode, qc):
	"""Toggle the QC hold on a just-counted coil's Bin Inventory row."""
	_require_count_role()
	coil_barcode = (coil_barcode or "").strip()
	qc = 1 if cint(qc) else 0
	if coil_barcode and frappe.db.exists("Bin Inventory", coil_barcode):
		frappe.db.set_value("Bin Inventory", coil_barcode, "qc", qc, update_modified=False)
		frappe.db.commit()
	return {"ok": True, "coil_barcode": coil_barcode, "qc": qc}


@frappe.whitelist()
def count_remove(bin_code, coil_barcode, client_event_id=None):
	"""Undo a mis-scan: retire the coil's In-app scan(s) in this bin and re-derive.

	Marks the exact transaction (by client_event_id) when known, else this
	operator's In-app scans of the coil into this bin, as `reversed`, then
	recomputes the coil's current location from the live transactions that remain
	(latest-wins). A coil with no remaining transactions is unmapped only if its
	Bin Inventory row was created in-app — SAP-fed rows are never deleted by a
	count undo.

	Rows are NEVER deleted (same rule as rm_remove): the row may already have
	been pushed to SAP — the record and its reconcile note must survive — and
	keeping the txn_key makes an offline replay of the undone scan a no-op
	instead of a resurrection. count_push.on_count_removed sets the SAP side of
	the story (Not Required, or Sent + note).
	"""
	_require_count_role()
	coil_barcode = (coil_barcode or "").strip()
	bin_code = (bin_code or "").strip()
	if not coil_barcode:
		return {"ok": True}
	# Both branches are scoped to transaction_type. Without it, the fallback filter
	# (coil + bin + In-app) also matches RM Movement rows — an Inventory Recorder who
	# counted a coil in a bin that happens to be an RM destination would silently
	# retire that coil's RM ledger history. `reversed: 0` keeps a repeated undo (or
	# a replayed count_remove) from re-reversing rows and reading a stale
	# previous_bin off an already-undone scan.
	if client_event_id:
		filters = {"txn_key": client_event_id, "transaction_type": COUNT_TXN_TYPE,
				   "reversed": 0}
	else:
		filters = {"coil_barcode": coil_barcode, "bin_code": bin_code, "source": "In-app",
				   "transaction_type": COUNT_TXN_TYPE, "reversed": 0}
	rows = frappe.get_all("Coil Transaction", filters=filters,
						  fields=["name", "raw_json"], order_by="creation asc")
	# The location before the *earliest* removed scan — the true "before" state.
	fallback_bin = None
	if rows:
		try:
			fallback_bin = (json.loads(rows[0].raw_json or "{}") or {}).get("previous_bin")
		except (ValueError, TypeError):
			fallback_bin = None
	now = now_datetime()
	for r in rows:
		frappe.db.set_value("Coil Transaction", r.name,
							{"reversed": 1, "reversed_by": frappe.session.user, "reversed_at": now},
							update_modified=False)
		count_push.on_count_removed(r.name)
	_rederive_coil(coil_barcode, fallback_bin)
	frappe.db.commit()
	return {"ok": True, "coil_barcode": coil_barcode, "current_bin": _current_bin(coil_barcode)}


def _rederive_coil(coil, fallback_bin=None):
	"""Recompute a coil's Bin Inventory row after transactions were removed.

	Order of truth: (1) the latest remaining In-app/other transaction, force-written
	(bypassing the latest-wins guard, which would refuse to move *back*);
	(2) else the `fallback_bin` captured from the removed scan (the coil's location
	before it) — this is how a coil fed by SAP is safely restored; (3) else the
	coil had no prior location at all (brand-new mis-scan) → drop the row.

	RM Movement rows are excluded: they carry no bin_code, and step (1) writes
	`bin_code` unconditionally, so an unscoped query could pick an RM row as
	"latest" and blank a mandatory field. The exclusion is raw SQL with COALESCE
	because the SAP feed leaves transaction_type NULL and `NULL NOT IN (...)` is
	NULL in SQL — a plain NOT IN would drop every feed row and lose the location
	history this function exists to restore.

	Reversed rows are ignored too: an undone count scan is kept for SAP
	reconciliation and replay idempotency, not as a statement of location."""
	latest = frappe.db.sql("""
		SELECT bin_code, qc, scanned_at
		FROM `tabCoil Transaction`
		WHERE coil_barcode = %(coil)s
		  AND COALESCE(transaction_type, '') NOT IN %(rm)s
		  AND reversed = 0
		ORDER BY scanned_at DESC, creation DESC
		LIMIT 1""", {"coil": coil, "rm": RM_TXN_TYPES}, as_dict=True)
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
	frappe.enqueue("sbx_wmslite.bin_api.process_pending_transactions",
				   queue="short", enqueue_after_commit=True)
	bin_code = (bin_code or "").strip()
	total = frappe.db.count("Bin Inventory", {"bin_code": bin_code}) if bin_code else 0
	return {"ok": True, "bin_code": bin_code, "count": total}
