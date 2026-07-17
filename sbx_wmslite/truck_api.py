"""Truck-level loading (the 'By Truck' loading model).

A truck can carry several SAP shipments at once. The data model stays
per-shipment (one Loading Plan per sap_plan_id — required for per-delivery goods
issue + T1), but when Loading Model = 'By Truck' the operator app groups every
open shipment for the same truck + delivery date into ONE loading task.

A "bundle" is that group, addressed by (truck_number, delivery_date). These
endpoints aggregate/claim/scan/complete across the bundle's plans by delegating
to the per-plan endpoints in api.py — so goods-issue, T1 push, audit and
idempotency all stay exactly as they are per shipment.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint

from sbx_wmslite import api
from sbx_wmslite.decode import decode_coil_id

OPEN_STATES = api.OPEN_STATES
DONE_STATES = api.DONE_STATES


# --------------------------------------------------------------------------
# grouping helpers
# --------------------------------------------------------------------------
def _dd(v):
	return str(v) if v else ""


def _bundle_plan_names(truck, delivery_date, states):
	"""Open (or done) plan names for a truck + delivery date, oldest first."""
	truck = (truck or "").strip()
	dd = (delivery_date or "").strip()
	rows = frappe.get_all(
		"Loading Plan",
		filters={"truck_number": truck, "plan_status": ["in", states]},
		fields=["name", "delivery_date", "received_at"],
		order_by="received_at asc", limit_page_length=0)
	return [r.name for r in rows if _dd(r.delivery_date) == dd]


def _agg_status(statuses):
	"""One label for the whole truck from its shipments' statuses."""
	s = set(statuses)
	if s & {"Pending Approval"}:
		return "Pending Approval"
	if s & {"Open", "In Progress", "Ready to Complete"}:
		return "In Progress"
	if s and s <= set(DONE_STATES):
		return "Completed"
	return next(iter(s), "Open")


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------
@frappe.whitelist()
def get_truck_bundle(truck, delivery_date=None, scope="open"):
	"""Combined loading view for a truck + delivery date.

	scope 'open' = plans still being loaded; 'done' = completed-today summary.
	Returns aggregate counts, the shipment list, and a combined coil list where
	each coil carries its owning `plan` + `sap_plan_id`.
	"""
	states = DONE_STATES if scope == "done" else OPEN_STATES
	names = _bundle_plan_names(truck, delivery_date, states)
	shipments, coils = [], []
	tot = {"total_coils": 0, "loaded_coils": 0, "pending_coils": 0, "skipped_coils": 0}
	claimed_by, statuses, gi_status = None, [], None

	for name in names:
		doc = frappe.get_doc("Loading Plan", name)
		doc.check_permission("read")
		shipments.append({
			"plan": doc.name, "sap_plan_id": doc.sap_plan_id, "plan_status": doc.plan_status,
			"total_coils": doc.total_coils, "loaded_coils": doc.loaded_coils,
			"pending_coils": doc.pending_coils, "skipped_coils": doc.skipped_coils,
			"gi_status": doc.gi_status, "gi_sap_document": doc.gi_sap_document,
		})
		for f in tot:
			tot[f] += (doc.get(f) or 0)
		statuses.append(doc.plan_status)
		if doc.claimed_by:
			claimed_by = doc.claimed_by
		if doc.gi_status and doc.gi_status != "Not Required":
			gi_status = doc.gi_status
		for c in doc.coils:
			coils.append({
				"coil_barcode": c.coil_barcode, "material_grade": c.material_grade,
				"weight": c.weight, "coil_status": c.coil_status, "skip_reason": c.skip_reason,
				"unplanned": cint(c.unplanned), "loaded_by": c.loaded_by,
				"loaded_at": str(c.loaded_at) if c.loaded_at else None,
				"plan": doc.name, "sap_plan_id": doc.sap_plan_id,
			})

	return {
		"is_bundle": 1,
		"name": (truck or "") + "|" + _dd(delivery_date),
		"truck_number": truck,
		"delivery_date": _dd(delivery_date) or None,
		"plan_status": _agg_status(statuses),
		"claimed_by": claimed_by,
		"gi_status": gi_status,
		"shipments": shipments,
		"coils": coils,
		**tot,
	}


# --------------------------------------------------------------------------
# claim
# --------------------------------------------------------------------------
@frappe.whitelist()
def claim_truck_bundle(truck, delivery_date=None, force=0):
	"""Claim every open shipment on the truck to the current operator."""
	names = _bundle_plan_names(truck, delivery_date, OPEN_STATES)
	if not names:
		frappe.throw(_("No open shipments for this truck"))
	me = frappe.session.user
	# Conflict check first (unless forcing a takeover).
	if not cint(force):
		for name in names:
			cb = frappe.db.get_value("Loading Plan", name, "claimed_by")
			if cb and cb != me:
				return {"ok": False, "conflict": True, "claimed_by": cb}
	for name in names:
		api.claim_plan(name, force=1)
	return {"ok": True, **get_truck_bundle(truck, delivery_date)}


# --------------------------------------------------------------------------
# scan (resolve a coil across the truck's shipments)
# --------------------------------------------------------------------------
@frappe.whitelist()
def scan_coil_bundle(truck, delivery_date, raw_qr):
	"""Resolve a scanned coil against ALL open shipments on the truck."""
	coil_id = decode_coil_id(raw_qr)
	names = _bundle_plan_names(truck, delivery_date, OPEN_STATES)
	already = None
	for name in names:
		doc = frappe.get_doc("Loading Plan", name)
		for c in doc.coils:
			if c.coil_barcode == coil_id:
				if c.coil_status == "Loaded":
					already = c.coil_barcode
					continue
				return {"matched": True, "decoded_coil_id": coil_id, "plan": name,
						"coil": {"coil_barcode": c.coil_barcode, "material_grade": c.material_grade,
								 "weight": c.weight, "coil_status": c.coil_status,
								 "plan": name, "sap_plan_id": doc.sap_plan_id}}
	if already:
		return {"matched": False, "reason": "already_loaded", "coil_barcode": already, "decoded_coil_id": coil_id}
	# Not on any shipment — offer over-scan onto the earliest open shipment.
	return {"matched": False, "reason": "not_on_truck", "decoded_coil_id": coil_id,
			"allow_overscan": cint(api._settings().allow_overscan),
			"overscan_plan": names[0] if names else None}


# --------------------------------------------------------------------------
# complete / short-load (whole-truck)
# --------------------------------------------------------------------------
@frappe.whitelist()
def complete_truck_bundle(truck, delivery_date=None):
	"""Complete every open shipment on the truck (each fires its own GI + T1).

	Refuses if any coil is still pending — the caller must run the short-load
	flow instead. Idempotent: already-completed shipments are skipped.
	"""
	names = _bundle_plan_names(truck, delivery_date, OPEN_STATES)
	pending = []
	for name in names:
		doc = frappe.get_doc("Loading Plan", name)
		doc.recompute_counts()
		if doc.pending_coils > 0:
			pending += [{"coil_barcode": c.coil_barcode, "plan": name}
						for c in doc.coils if c.coil_status == "Pending"]
	if pending:
		return {"ok": False, "needs_short_load": True, "pending": pending}
	for name in names:
		st = frappe.db.get_value("Loading Plan", name, "plan_status")
		if st not in DONE_STATES:
			api.complete_plan(name)
	return {"ok": True, **get_truck_bundle(truck, delivery_date)}


@frappe.whitelist()
def request_short_load_bundle(truck, delivery_date, skips):
	"""Short-load across the truck: route each coil's skip reason to its plan."""
	skips = api._parse(skips)
	by_plan = {}
	# map each coil to its owning open plan
	names = _bundle_plan_names(truck, delivery_date, OPEN_STATES)
	coil_plan = {}
	for name in names:
		for c in frappe.get_doc("Loading Plan", name).coils:
			coil_plan[c.coil_barcode] = name
	for s in skips:
		plan = coil_plan.get(s.get("coil_barcode"))
		if plan:
			by_plan.setdefault(plan, []).append(s)
	for plan, plan_skips in by_plan.items():
		api.request_short_load(plan, json.dumps(plan_skips))
	return {"ok": True, "shipments": len(by_plan)}


@frappe.whitelist()
def approve_short_load_bundle(truck, delivery_date, pin, remarks=None):
	"""Approve short-load for every awaiting shipment on the truck with one PIN."""
	names = _bundle_plan_names(truck, delivery_date, OPEN_STATES)
	approved = 0
	for name in names:
		if frappe.db.get_value("Loading Plan", name, "plan_status") == "Pending Approval":
			api.approve_short_load(name, pin, remarks)
			approved += 1
	return {"ok": True, "approved": approved, **get_truck_bundle(truck, delivery_date)}


@frappe.whitelist()
def decline_short_load_bundle(truck, delivery_date, pin):
	"""Decline short-load for every awaiting shipment on the truck."""
	names = _bundle_plan_names(truck, delivery_date, OPEN_STATES)
	for name in names:
		if frappe.db.get_value("Loading Plan", name, "plan_status") == "Pending Approval":
			api.decline_short_load(name, pin)
	return {"ok": True, **get_truck_bundle(truck, delivery_date)}
