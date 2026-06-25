"""Bin Inventory — inbound feed + helpers.

Bin Inventory maps each coil to its bin location. It is pushed from the system
that records bins (authenticated with the shared X-WMSLite-Key header, same key
as the SAP loading-plan feed), with a console upload as fallback. Idempotent
upsert keyed on coil_barcode (the Bin Inventory docname).
"""

import json

import frappe
from frappe.utils import now_datetime

BIN_FIELDS = ("bin_code", "material_grade", "weight", "zone", "aisle")


def _auth():
	"""Validate the shared API key. Raises on failure."""
	expected = frappe.get_cached_doc("WMSLite Settings").get_password("sap_api_key", raise_exception=False)
	got = frappe.get_request_header("X-WMSLite-Key")
	if not expected or not got or got != expected:
		raise frappe.AuthenticationError("Invalid or missing X-WMSLite-Key")


def upsert_bin(coil_barcode, data, source="Push API"):
	"""Create or update one Bin Inventory row (keyed on coil_barcode)."""
	coil_barcode = (coil_barcode or "").strip()
	if not coil_barcode:
		return None
	if frappe.db.exists("Bin Inventory", coil_barcode):
		doc = frappe.get_doc("Bin Inventory", coil_barcode)
	else:
		doc = frappe.new_doc("Bin Inventory")
		doc.coil_barcode = coil_barcode
	for f in BIN_FIELDS:
		if f in data and data.get(f) not in (None, ""):
			setattr(doc, f, data.get(f))
	if data.get("status"):
		doc.status = data["status"]
	elif not doc.status:
		doc.status = "Available"
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

	action = (data.get("action") or "upsert").strip().lower()
	rows = data.get("rows") or data.get("coils") or []
	upserted, removed, errors = [], [], []
	for r in rows:
		code = (r.get("coil_barcode") or "").strip()
		if not code:
			continue
		try:
			if action == "remove":
				if frappe.db.exists("Bin Inventory", code):
					frappe.delete_doc("Bin Inventory", code, force=1, ignore_permissions=True)
					removed.append(code)
			else:
				upsert_bin(code, r, source="Push API")
				upserted.append(code)
		except Exception as e:
			errors.append(f"{code}: {e}")
	frappe.db.commit()
	return {"ok": True, "upserted": len(upserted), "removed": len(removed),
			"errors": errors}


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
