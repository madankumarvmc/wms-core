"""Inbound SAP integration for SBX WMSLite.

SAP POSTs a loading plan (truck + coils) to a single endpoint, authenticated
with a shared API key in the `X-WMSLite-Key` header (NOT a Frappe session).

Payload:
    {
      "sap_plan_id": "LP2026-00123",      # stable idempotency key
      "truck_number": "MH12AB1234",
      "action": "upsert" | "cancel",       # default upsert
      "coils": [
        {"coil_barcode": "C1", "material_grade": "...", "weight": 1234.5}, ...
      ]
    }

Semantics (locked in design):
  * Idempotent on sap_plan_id — re-send updates the same plan.
  * Authoritative re-sync — coils absent from the payload that are still
    Pending are removed (logged); LOADED coils are always protected.
  * A re-send to a terminal plan (Completed / Cancelled) is rejected + logged,
    never silently reopened.
  * Atomic per plan: the whole upsert commits or rolls back together.
  * Cross-plan duplicate coils (same coil open on another truck) are flagged.

Every request is recorded in WMSLite SAP Log.
"""

import json

import frappe
from frappe.utils import cint, now_datetime

CLOSED_STATES = ("Completed", "Completed (Short)", "Cancelled")


def _client_ip():
	try:
		return frappe.local.request_ip
	except Exception:
		return None


def _log(action, result, message, truck=None, sap_plan_id=None, plan=None,
		 coil_count=0, payload=None):
	try:
		frappe.get_doc({
			"doctype": "WMSLite SAP Log",
			"received_at": now_datetime(),
			"action": action,
			"result": result,
			"message": (message or "")[:1000],
			"truck_number": truck,
			"sap_plan_id": sap_plan_id,
			"loading_plan": plan,
			"coil_count": coil_count,
			"source_ip": _client_ip(),
			"payload": json.dumps(payload, indent=2) if payload is not None else None,
		}).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		# Logging must never break the integration response.
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "WMSLite SAP Log write failed")


def _authenticate():
	"""Validate the shared API key. Raises 401 on failure."""
	settings = frappe.get_cached_doc("WMSLite Settings")
	if not cint(settings.sap_enabled):
		frappe.throw("SAP intake is disabled", frappe.PermissionError)
	expected = settings.get_password("sap_api_key", raise_exception=False)
	got = frappe.get_request_header("X-WMSLite-Key")
	if not expected or not got or got != expected:
		raise frappe.AuthenticationError("Invalid or missing X-WMSLite-Key")


def _duplicate_coils(coil_barcodes, exclude_plan):
	"""Coils already Pending/Loaded on another OPEN plan → potential SAP error."""
	if not coil_barcodes:
		return []
	rows = frappe.db.sql(
		"""
		SELECT c.coil_barcode, c.parent AS plan
		FROM `tabLoading Plan Coil` c
		JOIN `tabLoading Plan` p ON p.name = c.parent
		WHERE c.coil_barcode IN %(codes)s
		  AND c.coil_status IN ('Pending','Loaded')
		  AND p.plan_status NOT IN ('Completed','Completed (Short)','Cancelled')
		  AND p.name != %(plan)s
		""",
		{"codes": tuple(coil_barcodes), "plan": exclude_plan or "__none__"},
		as_dict=True,
	)
	return rows


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_loading_plan():
	"""SAP push entry point. Returns a JSON summary; logs every call."""
	# --- parse ---
	try:
		body = frappe.request.get_data(as_text=True)
		data = json.loads(body) if body else {}
	except Exception:
		_log("Create", "Error", "Malformed JSON body")
		frappe.local.response["http_status_code"] = 400
		return {"ok": False, "error": "Malformed JSON body"}

	# --- auth ---
	try:
		_authenticate()
	except (frappe.AuthenticationError, frappe.PermissionError) as e:
		_log("Create", "Rejected", str(e), payload=data)
		frappe.local.response["http_status_code"] = 401
		return {"ok": False, "error": str(e)}

	sap_plan_id = (data.get("sap_plan_id") or "").strip()
	truck = (data.get("truck_number") or "").strip()
	action = (data.get("action") or "upsert").strip().lower()
	coils = data.get("coils") or []

	if not sap_plan_id and not truck:
		_log(action.title(), "Rejected", "Missing sap_plan_id and truck_number", payload=data)
		frappe.local.response["http_status_code"] = 422
		return {"ok": False, "error": "sap_plan_id or truck_number required"}

	# --- locate existing plan: sap_plan_id primary, (truck, open) fallback ---
	existing_name = None
	if sap_plan_id:
		existing_name = frappe.db.get_value("Loading Plan", {"sap_plan_id": sap_plan_id}, "name")
	if not existing_name and truck:
		existing_name = frappe.db.get_value(
			"Loading Plan",
			{"truck_number": truck, "plan_status": ["in", ("Open", "In Progress", "Pending Approval")]},
			"name",
		)

	try:
		if action == "cancel":
			return _cancel(existing_name, truck, sap_plan_id, data)
		return _upsert(existing_name, truck, sap_plan_id, coils, data)
	except Exception as e:
		frappe.db.rollback()
		_log(action.title(), "Error", f"{type(e).__name__}: {e}", truck=truck,
			 sap_plan_id=sap_plan_id, coil_count=len(coils), payload=data)
		frappe.log_error(frappe.get_traceback(), "WMSLite SAP receive failed")
		frappe.local.response["http_status_code"] = 500
		return {"ok": False, "error": str(e)}


def _cancel(existing_name, truck, sap_plan_id, data):
	if not existing_name:
		_log("Cancel", "Rejected", "No matching plan to cancel", truck=truck,
			 sap_plan_id=sap_plan_id, payload=data)
		frappe.local.response["http_status_code"] = 404
		return {"ok": False, "error": "No matching plan to cancel"}

	doc = frappe.get_doc("Loading Plan", existing_name)
	if doc.plan_status in CLOSED_STATES:
		_log("Cancel", "Rejected", f"Plan already {doc.plan_status}", truck=truck,
			 sap_plan_id=sap_plan_id, plan=doc.name, payload=data)
		return {"ok": False, "error": f"Plan already {doc.plan_status}", "plan": doc.name}

	doc.plan_status = "Cancelled"
	doc.save(ignore_permissions=True)
	frappe.get_doc({
		"doctype": "Coil Load Event", "loading_plan": doc.name, "truck_number": doc.truck_number,
		"event_type": "Cancel", "result": "Success", "event_time": now_datetime(),
		"reason": "Cancelled via SAP push",
	}).insert(ignore_permissions=True)
	frappe.db.commit()
	_log("Cancel", "Accepted", "Plan cancelled", truck=doc.truck_number,
		 sap_plan_id=sap_plan_id, plan=doc.name, payload=data)
	return {"ok": True, "plan": doc.name, "status": doc.plan_status}


def _upsert(existing_name, truck, sap_plan_id, coils, data):
	incoming = {}
	for c in coils:
		code = (c.get("coil_barcode") or "").strip()
		if code:
			incoming[code] = c

	warnings = []
	dups = _duplicate_coils(list(incoming.keys()), existing_name)
	if dups:
		warnings.append("Coils already open on another plan: " +
						", ".join(f"{d.coil_barcode}@{d.plan}" for d in dups))

	if existing_name:
		doc = frappe.get_doc("Loading Plan", existing_name)
		if doc.plan_status in CLOSED_STATES:
			_log("Update", "Rejected", f"Plan is {doc.plan_status}; re-send ignored",
				 truck=truck, sap_plan_id=sap_plan_id, plan=doc.name, payload=data)
			return {"ok": False, "error": f"Plan is {doc.plan_status}", "plan": doc.name}
		action_label = "Update"
	else:
		doc = frappe.new_doc("Loading Plan")
		doc.sap_plan_id = sap_plan_id or None
		doc.truck_number = truck
		doc.source = "SAP API"
		doc.received_at = now_datetime()
		action_label = "Create"

	if truck:
		doc.truck_number = truck

	# --- authoritative reconcile ---
	allow_remove = cint(frappe.get_cached_doc("WMSLite Settings").allow_plan_remove_on_resync)
	existing_by_code = {c.coil_barcode: c for c in doc.coils}
	removed, added, protected = [], [], []

	# Remove still-Pending coils absent from the payload (Loaded are protected).
	if action_label == "Update":
		keep = []
		for c in doc.coils:
			if c.coil_barcode in incoming:
				keep.append(c)
			elif c.coil_status == "Pending" and allow_remove:
				removed.append(c.coil_barcode)
			else:
				if c.coil_barcode not in incoming and c.coil_status != "Pending":
					protected.append(c.coil_barcode)
				keep.append(c)
		doc.coils = keep
		existing_by_code = {c.coil_barcode: c for c in doc.coils}

	# Add / update incoming coils (never touch a Loaded row's status).
	for code, c in incoming.items():
		row = existing_by_code.get(code)
		if row:
			if row.coil_status != "Loaded":
				row.material_grade = c.get("material_grade")
				row.weight = c.get("weight")
				row.coil_qr_raw = c.get("coil_qr_raw")
		else:
			doc.append("coils", {
				"coil_barcode": code,
				"coil_qr_raw": c.get("coil_qr_raw"),
				"material_grade": c.get("material_grade"),
				"weight": c.get("weight"),
				"coil_status": "Pending",
			})
			added.append(code)

	doc.save(ignore_permissions=True)

	if removed:
		frappe.get_doc({
			"doctype": "Coil Load Event", "loading_plan": doc.name,
			"truck_number": doc.truck_number, "event_type": "Plan Amend",
			"result": "Success", "event_time": now_datetime(),
			"reason": f"SAP re-sync removed pending coils: {', '.join(removed)}",
		}).insert(ignore_permissions=True)

	frappe.db.commit()

	msg = f"{action_label}: +{len(added)} added, -{len(removed)} removed"
	if protected:
		msg += f", {len(protected)} protected"
	if warnings:
		msg += " | " + "; ".join(warnings)
	_log(action_label, "Accepted", msg, truck=doc.truck_number, sap_plan_id=sap_plan_id,
		 plan=doc.name, coil_count=len(incoming), payload=data)

	return {
		"ok": True,
		"plan": doc.name,
		"status": doc.plan_status,
		"total_coils": doc.total_coils,
		"added": added,
		"removed": removed,
		"protected": protected,
		"warnings": warnings,
	}
