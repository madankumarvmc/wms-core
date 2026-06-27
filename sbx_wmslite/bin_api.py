"""Bin Inventory — inbound feed + helpers.

Bin Inventory maps each coil to its bin location. It is pushed from the system
that records bins (authenticated with the shared X-WMSLite-Key header, same key
as the SAP loading-plan feed), with a console upload as fallback. Idempotent
upsert keyed on coil_barcode (the Bin Inventory docname).
"""

import json

import frappe
from frappe.utils import get_datetime, now_datetime

BIN_FIELDS = ("bin_code", "material_grade", "weight", "zone", "aisle",
			  "plant", "product", "sized")


def _truthy(v):
	"""QC flag from the feed may be true/1/'X'/'QC' — treat all as a hold."""
	return str(v).strip().lower() in ("1", "true", "x", "qc", "yes")


# Raw SAP payload field names -> our Bin Inventory fields. Lets the caller POST
# the SAME array it sends SAP; WMS Core does the mapping (not the middleware).
_SAP_KEYMAP = {
	"BATCH": "coil_barcode", "LOCATION": "bin_code", "WEIGHT": "weight",
	"MATERIAL": "material_grade", "PRODUCT": "product", "SIZED": "sized",
	"PLANT": "plant", "QC": "qc",
}


def _parse_sap_dt(d, t):
	"""SAP ENTRYDATE (ddMMyyyy) + ENTRYTIME (HHmmss) -> datetime, or None."""
	d = str(d or "").strip()
	t = str(t or "").strip().rjust(6, "0")
	if len(d) != 8 or not d.isdigit():
		return None
	try:
		return get_datetime(f"{d[4:8]}-{d[2:4]}-{d[0:2]} {t[0:2]}:{t[2:4]}:{t[4:6]}")
	except Exception:
		return None


def _normalize_sap_row(r):
	"""Accept a raw SAP row (BATCH/LOCATION/ENTRYDATE/...) and map it onto our
	field names. Native keys (coil_barcode, bin_code, scanned_at) win if present,
	so both the raw-SAP and the native payload shapes work."""
	if not isinstance(r, dict):
		return {}
	out = dict(r)
	for sap, ours in _SAP_KEYMAP.items():
		if r.get(sap) not in (None, "") and not out.get(ours):
			out[ours] = r[sap]
	if not out.get("scanned_at") and r.get("ENTRYDATE"):
		dt = _parse_sap_dt(r.get("ENTRYDATE"), r.get("ENTRYTIME"))
		if dt:
			out["scanned_at"] = dt
	return out


def _auth():
	"""Validate the shared API key. Raises on failure."""
	expected = frappe.get_cached_doc("WMSLite Settings").get_password("sap_api_key", raise_exception=False)
	got = frappe.get_request_header("X-WMSLite-Key")
	if not expected or not got or got != expected:
		raise frappe.AuthenticationError("Invalid or missing X-WMSLite-Key")


def upsert_bin(coil_barcode, data, source="Push API"):
	"""Create or update one Bin Inventory row (keyed on coil_barcode).

	The feed sends one record per scan (transaction-style); we keep a single row
	per coil. Latest-scan-wins: if the stored row already has a newer `scanned_at`
	than the incoming scan, the update is skipped so an out-of-order or older scan
	can never overwrite a newer location. Returns the docname, or None if the
	coil is blank or the incoming scan is stale.
	"""
	coil_barcode = (coil_barcode or "").strip()
	if not coil_barcode:
		return None

	incoming_ts = get_datetime(data["scanned_at"]) if data.get("scanned_at") else None

	if frappe.db.exists("Bin Inventory", coil_barcode):
		doc = frappe.get_doc("Bin Inventory", coil_barcode)
		if incoming_ts and doc.scanned_at and incoming_ts < get_datetime(doc.scanned_at):
			return None  # stale scan — keep the newer one
	else:
		doc = frappe.new_doc("Bin Inventory")
		doc.coil_barcode = coil_barcode

	for f in BIN_FIELDS:
		if f in data and data.get(f) not in (None, ""):
			setattr(doc, f, data.get(f))
	if "qc" in data:
		doc.qc = 1 if _truthy(data.get("qc")) else 0
	if data.get("status"):
		doc.status = data["status"]
	elif not doc.status:
		doc.status = "Available"
	if incoming_ts:
		doc.scanned_at = incoming_ts
	doc.source = source
	doc.updated_at = now_datetime()
	doc.save(ignore_permissions=True)
	return doc.name


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_bin_inventory():
	"""Push entry point. Payload: {action?: upsert|remove, rows:[{coil_barcode, bin_code, ...}]}."""
	try:
		body = frappe.request.get_data(as_text=True)
		data = json.loads(body) if body else {}
	except Exception:
		frappe.local.response["http_status_code"] = 400
		return {"ok": False, "error": "Malformed JSON body"}

	try:
		_auth()
	except frappe.AuthenticationError as e:
		frappe.local.response["http_status_code"] = 401
		return {"ok": False, "error": str(e)}

	# Accept either {action, rows:[...]} or a bare array (the raw SAP payload).
	if isinstance(data, list):
		action, rows = "upsert", data
	else:
		action = (data.get("action") or "upsert").strip().lower()
		rows = data.get("rows") or data.get("coils") or []

	upserted, removed, stale, errors = [], [], [], []
	for raw in rows:
		r = _normalize_sap_row(raw)
		code = (r.get("coil_barcode") or "").strip()
		if not code:
			continue
		try:
			if action == "remove":
				if frappe.db.exists("Bin Inventory", code):
					frappe.delete_doc("Bin Inventory", code, force=1, ignore_permissions=True)
					removed.append(code)
			elif upsert_bin(code, r, source="Push API"):
				upserted.append(code)
			else:
				stale.append(code)  # older scan than what we already hold
		except Exception as e:
			errors.append(f"{code}: {e}")
	frappe.db.commit()
	return {"ok": True, "upserted": len(upserted), "removed": len(removed),
			"stale": len(stale), "errors": errors}


# --------------------------------------------------------------------------
# coil transactions (raw scan log) -> derived Bin Inventory
# --------------------------------------------------------------------------
TXN_FIELDS = ("coil_barcode", "bin_code", "weight", "material_grade",
			  "product", "sized", "plant", "scanned_at", "transaction_type",
			  "username")


def _txn_key(raw, norm):
	"""Idempotency key per scan: coil + ENTRYDATE + ENTRYTIME (matches SAP)."""
	coil = (norm.get("coil_barcode") or "").strip()
	if not coil:
		return None
	d = str(raw.get("ENTRYDATE") or "").strip()
	t = str(raw.get("ENTRYTIME") or "").strip()
	if d:
		return f"{coil}_{d}_{t}"
	sa = norm.get("scanned_at")
	if sa:
		return f"{coil}_{get_datetime(sa).strftime('%Y%m%d%H%M%S')}"
	return None


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_coil_transactions():
	"""Raw scan intake. POST the SAME array sent to SAP (a bare array, or
	{rows:[...]}). Each scan is stored as a Coil Transaction (idempotent on
	coil+date+time); Bin Inventory is then derived asynchronously (latest wins).
	"""
	try:
		body = frappe.request.get_data(as_text=True)
		data = json.loads(body) if body else {}
	except Exception:
		frappe.local.response["http_status_code"] = 400
		return {"ok": False, "error": "Malformed JSON body"}

	try:
		_auth()
	except frappe.AuthenticationError as e:
		frappe.local.response["http_status_code"] = 401
		return {"ok": False, "error": str(e)}

	rows = data if isinstance(data, list) else (data.get("rows") or data.get("coils") or [])
	inserted, duplicate, errors = [], [], []
	for raw in rows:
		norm = _normalize_sap_row(raw)
		coil = (norm.get("coil_barcode") or "").strip()
		if not coil:
			continue
		key = _txn_key(raw, norm)
		try:
			if key and frappe.db.exists("Coil Transaction", {"txn_key": key}):
				duplicate.append(coil)
				continue
			doc = frappe.new_doc("Coil Transaction")
			for f in TXN_FIELDS:
				if norm.get(f) not in (None, ""):
					setattr(doc, f, norm.get(f))
			doc.qc = 1 if _truthy(norm.get("qc")) else 0
			doc.txn_key = key
			doc.source = "Push API"
			doc.raw_json = json.dumps(raw)
			doc.insert(ignore_permissions=True)
			inserted.append(doc.name)
		except Exception as e:
			errors.append(f"{coil}: {e}")
	frappe.db.commit()

	if inserted:
		frappe.enqueue("sbx_wmslite.bin_api.process_pending_transactions",
					   queue="short", enqueue_after_commit=True)
	return {"ok": True, "inserted": len(inserted), "duplicate": len(duplicate),
			"errors": errors}


def process_pending_transactions(limit=2000):
	"""Derive Bin Inventory from unprocessed Coil Transactions, oldest scan first
	so the latest scan wins. Marks each transaction processed. Idempotent and
	safe to run from both the post-intake job and the scheduled sweep."""
	pending = frappe.get_all(
		"Coil Transaction", filters={"processed": 0},
		fields=["name", "coil_barcode", "bin_code", "weight", "material_grade",
				"product", "sized", "plant", "qc", "scanned_at"],
		order_by="scanned_at asc, creation asc", limit_page_length=limit)
	done = 0
	for t in pending:
		try:
			upsert_bin(t.coil_barcode, {
				"bin_code": t.bin_code, "weight": t.weight,
				"material_grade": t.material_grade, "product": t.product,
				"sized": t.sized, "plant": t.plant, "qc": t.qc,
				"scanned_at": t.scanned_at,
			}, source="Push API")
			bin_name = t.coil_barcode if frappe.db.exists("Bin Inventory", t.coil_barcode) else None
			frappe.db.set_value("Coil Transaction", t.name,
								{"processed": 1, "processed_at": now_datetime(),
								 "bin_inventory": bin_name}, update_modified=False)
			done += 1
		except Exception:
			frappe.log_error(frappe.get_traceback(), "WMSLite txn derive failed")
	frappe.db.commit()
	return {"processed": done, "scanned": len(pending)}


# --------------------------------------------------------------------------
# plan resolution (used by the operator picking flow, Phase 3)
# --------------------------------------------------------------------------
def resolve_plan_bins(plan):
	"""Stamp each coil's current bin (from Bin Inventory) onto the plan. Returns
	True if any coil changed, so the caller can decide whether to save."""
	changed = False
	codes = [c.coil_barcode for c in plan.coils]
	if not codes:
		return False
	locs = {b.name: b for b in frappe.get_all(
		"Bin Inventory", filters={"coil_barcode": ["in", codes]},
		fields=["name", "bin_code", "zone", "aisle"])}
	for c in plan.coils:
		b = locs.get(c.coil_barcode)
		new_bin = b.bin_code if b else None
		new_zone = b.zone if b else None
		new_aisle = b.aisle if b else None
		if (c.bin_code, c.zone, c.aisle) != (new_bin, new_zone, new_aisle):
			c.bin_code, c.zone, c.aisle = new_bin, new_zone, new_aisle
			changed = True
	return changed
