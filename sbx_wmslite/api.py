"""Operator-facing (PWA) endpoints for SBX WMSLite.

Flow: look up truck -> claim plan -> scan coil QR -> confirm load -> repeat.
Plus the exception paths: undo, over-scan, short-load request + online PIN
approval, and claim takeover.

All endpoints run under the logged-in operator's session (CSRF-protected) and
rely on Loading Operator / Loading Supervisor DocType permissions. Every state
change writes an immutable Coil Load Event for audit and offline idempotency.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, now_datetime, today

from sbx_wmslite.decode import decode_coil_id

OPEN_STATES = ("Open", "In Progress", "Ready to Complete", "Pending Approval")
DONE_STATES = ("Completed", "Completed (Short)")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _parse(data):
	return json.loads(data) if isinstance(data, str) else (data or {})


def _settings():
	return frappe.get_cached_doc("WMSLite Settings")


def _log_event(plan, event_type, **kw):
	"""Create a Coil Load Event. Idempotent on client_event_id when supplied."""
	cid = kw.get("client_event_id")
	if cid:
		existing = frappe.db.get_value("Coil Load Event", {"client_event_id": cid}, "name")
		if existing:
			return frappe.get_doc("Coil Load Event", existing)

	ev = frappe.new_doc("Coil Load Event")
	ev.loading_plan = plan.name if plan else None
	ev.truck_number = plan.truck_number if plan else kw.get("truck_number")
	ev.event_time = now_datetime()
	ev.operator = frappe.session.user
	for f in ("coil_barcode", "scanned_qr", "decoded_coil_id", "event_type",
			  "result", "reason", "approver", "synced_offline", "client_event_id"):
		if f in kw:
			setattr(ev, f, kw[f])
	ev.event_type = event_type
	ev.insert(ignore_permissions=True)
	return ev


def _coil_row(plan, coil_barcode):
	for c in plan.coils:
		if c.coil_barcode == coil_barcode:
			return c
	return None


def _plan_progress(plan):
	return {
		"name": plan.name,
		"truck_number": plan.truck_number,
		"sap_plan_id": plan.sap_plan_id,
		"delivery_date": str(plan.delivery_date) if plan.get("delivery_date") else None,
		"plan_status": plan.plan_status,
		"total_coils": plan.total_coils,
		"loaded_coils": plan.loaded_coils,
		"skipped_coils": plan.skipped_coils,
		"pending_coils": plan.pending_coils,
		"claimed_by": plan.claimed_by,
	}


# --------------------------------------------------------------------------
# lookup & claim
# --------------------------------------------------------------------------
@frappe.whitelist()
def lookup_trucks(truck_number):
	"""Return open/in-progress plans whose truck matches (exact or prefix)."""
	truck_number = (truck_number or "").strip()
	if not truck_number:
		return []
	rows = frappe.get_all(
		"Loading Plan",
		filters={"truck_number": ["like", f"{truck_number}%"], "plan_status": ["in", OPEN_STATES]},
		fields=["name", "truck_number", "sap_plan_id", "delivery_date", "plan_status", "total_coils",
				"loaded_coils", "pending_coils", "skipped_coils", "claimed_by"],
		order_by="received_at desc",
		limit_page_length=25,
	)
	if (_settings().loading_group_mode or "By Shipment") == "By Truck":
		return _group_by_truck(rows)
	return rows


@frappe.whitelist()
def get_plan(plan):
	"""Full plan + coils for the loading screen.

	When bin-directed picking is enabled, the coils' current bin locations are
	refreshed from Bin Inventory (persisted so reports stay in sync) and the
	response flags `bin_picking` so the PWA shows the bins-to-visit flow.
	"""
	doc = frappe.get_doc("Loading Plan", plan)
	doc.check_permission("read")

	s = _settings()
	bin_enabled = cint(s.bin_picking_enabled)
	if bin_enabled:
		from sbx_wmslite.bin_api import resolve_plan_bins
		if resolve_plan_bins(doc):
			doc.save(ignore_permissions=True)
			frappe.db.commit()

	out = _plan_progress(doc)
	out["claimed_at"] = str(doc.claimed_at) if doc.claimed_at else None
	out["gi_status"] = doc.gi_status
	out["gi_sap_document"] = doc.gi_sap_document
	out["completed_at"] = str(doc.completed_at) if doc.completed_at else None
	out["coils"] = [
		{
			"coil_barcode": c.coil_barcode,
			"material_grade": c.material_grade,
			"weight": c.weight,
			"coil_status": c.coil_status,
			"skip_reason": c.skip_reason,
			"unplanned": cint(c.unplanned),
			"loaded_by": c.loaded_by,
			"loaded_at": str(c.loaded_at) if c.loaded_at else None,
			"bin_code": c.get("bin_code"),
			"zone": c.get("zone"),
			"aisle": c.get("aisle"),
		}
		for c in doc.coils
	]
	# Active only when enabled AND this truck actually has at least one located coil.
	out["bin_picking"] = 1 if (bin_enabled and any(c.get("bin_code") for c in doc.coils)) else 0
	return out


def _truck_key(p):
	return (p.get("truck_number") or "", str(p.get("delivery_date") or ""))


def _group_by_truck(plans):
	"""Collapse per-shipment plans into truck groups (key = truck + delivery date).
	Each group aggregates coil counts and lists its shipments."""
	groups, order = {}, []
	for p in plans:
		k = _truck_key(p)
		g = groups.get(k)
		if not g:
			g = {
				"is_group": 1,
				"truck_number": p.get("truck_number"),
				"delivery_date": str(p["delivery_date"]) if p.get("delivery_date") else None,
				"shipment_count": 0, "sap_plan_ids": [], "plans": [],
				"total_coils": 0, "loaded_coils": 0, "pending_coils": 0, "skipped_coils": 0,
				"claimed_by": None, "statuses": [], "gi_status": None,
			}
			groups[k] = g
			order.append(k)
		g["shipment_count"] += 1
		g["plans"].append(p.get("name"))
		if p.get("sap_plan_id"):
			g["sap_plan_ids"].append(p.get("sap_plan_id"))
		for f in ("total_coils", "loaded_coils", "pending_coils", "skipped_coils"):
			g[f] += (p.get(f) or 0)
		if p.get("claimed_by"):
			g["claimed_by"] = p.get("claimed_by")
		g["statuses"].append(p.get("plan_status"))
		if p.get("gi_status") and p.get("gi_status") != "Not Required":
			g["gi_status"] = p.get("gi_status")
	# name = synthetic bundle id "truck|date" so the PWA can address the group
	out = []
	for k in order:
		g = groups[k]
		g["name"] = (g["truck_number"] or "") + "|" + (g["delivery_date"] or "")
		st = set(g["statuses"])
		if "Pending Approval" in st:
			g["plan_status"] = "Pending Approval"
		elif st & {"Open", "In Progress", "Ready to Complete"}:
			g["plan_status"] = "In Progress"
		elif st and st <= set(DONE_STATES):
			g["plan_status"] = "Completed"
		else:
			g["plan_status"] = next(iter(st), "Open")
		out.append(g)
	return out


@frappe.whitelist()
def operator_home():
	"""Landing payload for the operator PWA: pending work (FIFO), completed today,
	and a stats strip. When Loading Model = By Truck, per-shipment plans are grouped
	into truck cards."""
	mode = _settings().loading_group_mode or "By Shipment"
	today0 = today() + " 00:00:00"
	pending = frappe.get_all(
		"Loading Plan",
		filters={"plan_status": ["in", OPEN_STATES]},
		fields=["name", "truck_number", "sap_plan_id", "delivery_date", "plan_status",
				"total_coils", "loaded_coils", "pending_coils", "skipped_coils", "claimed_by", "received_at"],
		order_by="received_at asc",  # oldest first = FIFO dispatch
		limit_page_length=0,
	)
	loaded = frappe.get_all(
		"Loading Plan",
		filters={"plan_status": ["in", DONE_STATES], "completed_at": [">=", today0]},
		fields=["name", "truck_number", "sap_plan_id", "delivery_date", "plan_status", "total_coils",
				"loaded_coils", "skipped_coils", "gi_status", "completed_at"],
		order_by="completed_at desc",
		limit_page_length=0,
	)
	if mode == "By Truck":
		pending = _group_by_truck(pending)
		loaded = _group_by_truck(loaded)
	stats = {
		"pending": len(pending),
		"loaded_today": len(loaded),
		"coils_today": frappe.db.count("Coil Load Event",
			{"event_type": "Loaded", "event_time": [">=", today0]}),
	}
	return {"mode": mode, "pending": pending, "loaded": loaded, "stats": stats}


@frappe.whitelist()
def claim_plan(plan, force=0):
	"""Soft-claim a truck to the current operator.

	If already claimed by someone else, returns a conflict unless `force` (a
	takeover). Takeovers are logged so the floor has an audit trail.
	"""
	doc = frappe.get_doc("Loading Plan", plan)
	doc.check_permission("write")
	me = frappe.session.user

	if doc.claimed_by and doc.claimed_by != me and not cint(force):
		return {"ok": False, "conflict": True, "claimed_by": doc.claimed_by,
				"claimed_at": str(doc.claimed_at) if doc.claimed_at else None}

	took_over = bool(doc.claimed_by and doc.claimed_by != me)
	doc.claimed_by = me
	doc.claimed_at = now_datetime()
	doc.save(ignore_permissions=True)
	_log_event(doc, "Takeover" if took_over else "Claim",
			   reason=f"Taken over from {doc.claimed_by}" if took_over else None)
	frappe.db.commit()
	return {"ok": True, "took_over": took_over, **_plan_progress(doc)}


# --------------------------------------------------------------------------
# scan & confirm
# --------------------------------------------------------------------------
@frappe.whitelist()
def scan_coil(plan, raw_qr):
	"""Resolve a scanned QR against the plan. No state change (confirm does that).

	Returns match outcome so the PWA can show the coil card or a reject banner.
	Rejections are NOT logged here (a scan is cheap and noisy); the reject is
	logged only if the operator's device reports it, keeping the audit clean.
	"""
	doc = frappe.get_doc("Loading Plan", plan)
	doc.check_permission("read")
	coil_id = decode_coil_id(raw_qr)
	row = _coil_row(doc, coil_id)

	if not row:
		return {"matched": False, "reason": "not_on_plan", "decoded_coil_id": coil_id,
				"allow_overscan": cint(_settings().allow_overscan)}
	if row.coil_status == "Loaded":
		return {"matched": False, "reason": "already_loaded", "decoded_coil_id": coil_id,
				"coil_barcode": row.coil_barcode}
	return {
		"matched": True,
		"decoded_coil_id": coil_id,
		"coil": {
			"coil_barcode": row.coil_barcode,
			"material_grade": row.material_grade,
			"weight": row.weight,
			"coil_status": row.coil_status,
		},
	}


@frappe.whitelist()
def confirm_load(plan, coil_barcode, scanned_qr=None, client_event_id=None, synced_offline=0):
	"""Mark a coil Loaded. Idempotent on client_event_id (safe offline replay)."""
	if client_event_id:
		dup = frappe.db.get_value("Coil Load Event", {"client_event_id": client_event_id},
								  ["name", "result"], as_dict=True)
		if dup:
			doc = frappe.get_doc("Loading Plan", plan)
			return {"ok": True, "idempotent": True, **_plan_progress(doc)}

	doc = frappe.get_doc("Loading Plan", plan)
	doc.check_permission("write")
	row = _coil_row(doc, coil_barcode)
	if not row:
		frappe.throw(_("Coil {0} is not on this plan").format(coil_barcode))
	if row.coil_status == "Loaded":
		return {"ok": True, "idempotent": True, **_plan_progress(doc)}

	ev = _log_event(doc, "Loaded", result="Success", coil_barcode=coil_barcode,
					scanned_qr=scanned_qr, decoded_coil_id=coil_barcode,
					client_event_id=client_event_id, synced_offline=cint(synced_offline))
	row.coil_status = "Loaded"
	row.loaded_by = frappe.session.user
	row.loaded_at = now_datetime()
	row.load_event = ev.name
	row.skip_reason = None
	if scanned_qr:
		# Keep the full scanned QR on the coil so the SAP T1 push can parse
		# plant/product/material/sized/weight out of it.
		row.coil_qr_raw = scanned_qr
	doc.save(ignore_permissions=True)
	_mark_bin_picked(coil_barcode)
	from sbx_wmslite import loading_push
	loading_push.on_coil_loaded(doc, row)
	frappe.db.commit()
	return {"ok": True, "coil_barcode": coil_barcode, "event": ev.name, **_plan_progress(doc)}


def _mark_bin_picked(coil_barcode):
	"""Flag the coil's Bin Inventory row as Picked once loaded (best-effort)."""
	if not cint(_settings().mark_picked_on_load):
		return
	try:
		if frappe.db.exists("Bin Inventory", coil_barcode):
			frappe.db.set_value("Bin Inventory", coil_barcode, "status", "Picked",
								update_modified=False)
	except Exception:
		frappe.log_error(f"mark_picked failed for {coil_barcode}", "WMSLite bin pick")


@frappe.whitelist()
def log_reject(plan, coil_barcode=None, scanned_qr=None, reason=None, client_event_id=None):
	"""Record a rejected scan (wrong truck / already loaded / unknown coil)."""
	doc = frappe.get_doc("Loading Plan", plan)
	doc.check_permission("read")
	ev = _log_event(doc, "Rejected", result="Rejected", coil_barcode=coil_barcode,
					scanned_qr=scanned_qr, reason=reason, client_event_id=client_event_id)
	frappe.db.commit()
	return {"ok": True, "event": ev.name}


# --------------------------------------------------------------------------
# exceptions: undo, over-scan
# --------------------------------------------------------------------------
@frappe.whitelist()
def undo_last_load(plan, coil_barcode, client_event_id=None):
	"""Revert a just-loaded coil to Pending (mis-scan), within the undo window."""
	doc = frappe.get_doc("Loading Plan", plan)
	doc.check_permission("write")
	row = _coil_row(doc, coil_barcode)
	if not row or row.coil_status != "Loaded":
		frappe.throw(_("Coil {0} is not currently loaded").format(coil_barcode))

	window = cint(_settings().undo_window_seconds) or 120
	if row.loaded_at:
		age = (now_datetime() - row.loaded_at).total_seconds()
		if age > window:
			frappe.throw(_("Undo window ({0}s) has passed for coil {1}").format(window, coil_barcode))

	from sbx_wmslite import loading_push
	loading_push.on_coil_unloaded(row)  # cancel a not-yet-sent SAP push
	if cint(row.unplanned):
		# Unplanned coil added by over-scan — undo removes it entirely.
		doc.coils.remove(row)
	else:
		row.coil_status = "Pending"
		row.loaded_by = None
		row.loaded_at = None
		row.load_event = None
	ev = _log_event(doc, "Undo", result="Success", coil_barcode=coil_barcode,
					client_event_id=client_event_id)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "event": ev.name, **_plan_progress(doc)}


@frappe.whitelist()
def overscan_add(plan, coil_barcode, scanned_qr=None, material_grade=None, weight=None,
				 client_event_id=None, synced_offline=0):
	"""Add a coil not on the plan and load it (over-scan). Gated by settings."""
	if not cint(_settings().allow_overscan):
		frappe.throw(_("Over-scan is disabled. Coil {0} is not on this plan.").format(coil_barcode))

	doc = frappe.get_doc("Loading Plan", plan)
	doc.check_permission("write")
	if _coil_row(doc, coil_barcode):
		return confirm_load(plan, coil_barcode, scanned_qr, client_event_id, synced_offline)

	ev = _log_event(doc, "Unplanned Add", result="Success", coil_barcode=coil_barcode,
					scanned_qr=scanned_qr, decoded_coil_id=coil_barcode,
					client_event_id=client_event_id, synced_offline=cint(synced_offline),
					reason="Coil not on SAP plan — added by over-scan")
	doc.append("coils", {
		"coil_barcode": coil_barcode,
		"coil_qr_raw": scanned_qr,
		"material_grade": material_grade,
		"weight": weight,
		"coil_status": "Loaded",
		"unplanned": 1,
		"loaded_by": frappe.session.user,
		"loaded_at": now_datetime(),
		"load_event": ev.name,
	})
	doc.save(ignore_permissions=True)
	from sbx_wmslite import loading_push
	loading_push.on_coil_loaded(doc, doc.coils[-1])
	frappe.db.commit()
	return {"ok": True, "event": ev.name, "unplanned": True, **_plan_progress(doc)}


# --------------------------------------------------------------------------
# short-load: request + online PIN approval / decline
# --------------------------------------------------------------------------
@frappe.whitelist()
def complete_plan(plan):
	"""Operator presses 'Complete Loading' on a fully-loaded plan.

	Only valid when nothing is pending — a short-load (finishing early with coils
	unloaded) must go through request_short_load + supervisor approval instead.
	Sets flags.operator_completing so the controller finalises the plan even when
	'Auto-complete When All Coils Loaded' is off; that transition fires the SAP
	goods-issue and releases the plan's Deferred T1 loading pushes (see
	LoadingPlan.on_update). Idempotent: a no-op if the plan is already complete."""
	doc = frappe.get_doc("Loading Plan", plan)
	doc.check_permission("write")
	if doc.plan_status in DONE_STATES:
		return {"ok": True, "already": True, **_plan_progress(doc)}
	if doc.plan_status == "Cancelled":
		frappe.throw(_("Plan {0} is cancelled").format(plan))
	if doc.plan_status == "Pending Approval":
		frappe.throw(_("Plan {0} is awaiting short-load approval").format(plan))
	# Recompute from the child rows so the pending guard can't be fooled by a
	# stale stored tally.
	doc.recompute_counts()
	if doc.total_coils == 0:
		frappe.throw(_("No coils on this plan"))
	if doc.pending_coils > 0:
		frappe.throw(_("{0} coil(s) still pending — request a short-load approval "
					   "to finish early").format(doc.pending_coils))
	doc.flags.operator_completing = True
	doc.save(ignore_permissions=True)  # recompute -> Completed / Completed (Short)
	_log_event(doc, "Completion", result="Success",
			   reason=f"{doc.loaded_coils} loaded, {doc.skipped_coils} skipped")
	frappe.db.commit()
	return {"ok": True, **_plan_progress(doc)}


@frappe.whitelist()
def request_short_load(plan, skips):
	"""Operator requests completion with coils still pending.

	`skips` = [{coil_barcode, reason, note}]. Sets those coils Skipped and moves
	the plan to Pending Approval. Final completion needs a supervisor PIN online.
	"""
	skips = _parse(skips)
	doc = frappe.get_doc("Loading Plan", plan)
	doc.check_permission("write")

	by_code = {s.get("coil_barcode"): s for s in skips}
	skipped = []
	for c in doc.coils:
		if c.coil_status == "Pending" and c.coil_barcode in by_code:
			s = by_code[c.coil_barcode]
			c.coil_status = "Skipped"
			c.skip_reason = s.get("reason") or "Other"
			c.skip_note = s.get("note")
			skipped.append(c.coil_barcode)

	doc.plan_status = "Pending Approval"
	doc.save(ignore_permissions=True)
	_log_event(doc, "Short-Load Request", result="Success",
			   reason=f"Skipping {len(skipped)} coil(s): {', '.join(skipped)}")
	frappe.db.commit()
	return {"ok": True, "skipped": skipped, **_plan_progress(doc)}


def _verify_supervisor_pin(pin):
	"""Online PIN check against WMSLite Settings (server-side, never cached)."""
	stored = _settings().get_password("supervisor_pin", raise_exception=False)
	return bool(stored) and str(pin) == str(stored)


@frappe.whitelist()
def approve_short_load(plan, pin, remarks=None):
	"""Supervisor approves a short-load at the handheld. Requires online PIN."""
	doc = frappe.get_doc("Loading Plan", plan)
	doc.check_permission("write")
	if doc.plan_status != "Pending Approval":
		frappe.throw(_("Plan {0} is not awaiting approval").format(plan))
	if not _verify_supervisor_pin(pin):
		_log_event(doc, "Approval", result="Rejected", reason="Invalid PIN")
		frappe.db.commit()
		frappe.throw(_("Invalid supervisor PIN"))

	# Force terminal status (recompute_counts leaves Pending Approval alone, so
	# clear it first, then let the controller settle the short/complete label).
	# operator_completing makes recompute finalise the plan even when auto-complete
	# is off — supervisor approval IS the explicit completion here, so it must not
	# bounce to 'Ready to Complete'.
	doc.flags.operator_completing = True
	doc.plan_status = "In Progress"
	doc.approved_by = frappe.session.user
	doc.approval_remarks = remarks
	doc.save(ignore_permissions=True)  # recompute -> Completed / Completed (Short)
	_log_event(doc, "Approval", result="Success", approver=frappe.session.user,
			   reason=remarks or "Short-load approved")
	frappe.db.commit()
	return {"ok": True, **_plan_progress(doc)}


@frappe.whitelist()
def decline_short_load(plan, pin):
	"""Supervisor declines the short-load — plan returns to In Progress."""
	doc = frappe.get_doc("Loading Plan", plan)
	doc.check_permission("write")
	if not _verify_supervisor_pin(pin):
		frappe.throw(_("Invalid supervisor PIN"))
	# Un-skip the coils so the operator can keep loading.
	for c in doc.coils:
		if c.coil_status == "Skipped":
			c.coil_status = "Pending"
			c.skip_reason = None
			c.skip_note = None
	doc.plan_status = "In Progress"
	doc.save(ignore_permissions=True)
	_log_event(doc, "Approval", result="Rejected", approver=frappe.session.user,
			   reason="Short-load declined")
	frappe.db.commit()
	return {"ok": True, **_plan_progress(doc)}


# --------------------------------------------------------------------------
# offline support
# --------------------------------------------------------------------------
@frappe.whitelist()
def get_client_config():
	"""Non-secret settings the PWA caches for offline decode/undo decisions."""
	s = _settings()
	return {
		"qr_decode_rule": s.qr_decode_rule or "Identity",
		"qr_decode_regex": s.qr_decode_regex,
		"allow_overscan": cint(s.allow_overscan),
		"undo_window_seconds": cint(s.undo_window_seconds) or 120,
		"bin_picking_enabled": cint(s.bin_picking_enabled),
		"bin_decode_rule": s.bin_decode_rule or "Identity",
		"bin_decode_regex": s.bin_decode_regex,
		"allow_pick_outside_bin": cint(s.allow_pick_outside_bin),
		"count_enabled": cint(s.count_enabled),
		"count_qc_default": s.count_qc_default or "OK",
		"loading_group_mode": s.loading_group_mode or "By Shipment",
		"completion_mode": s.completion_mode or "Whole Truck",
	}


@frappe.whitelist()
def submit_offline_queue(events):
	"""Replay a batch of queued operator events. Each is idempotent by id."""
	events = _parse(events)
	results = []
	for e in events:
		etype = e.get("type")
		try:
			if etype == "confirm_load":
				r = confirm_load(e["plan"], e["coil_barcode"], e.get("scanned_qr"),
								 e.get("client_event_id"), synced_offline=1)
			elif etype == "overscan_add":
				r = overscan_add(e["plan"], e["coil_barcode"], e.get("scanned_qr"),
								 e.get("material_grade"), e.get("weight"),
								 e.get("client_event_id"), synced_offline=1)
			elif etype == "undo":
				r = undo_last_load(e["plan"], e["coil_barcode"], e.get("client_event_id"))
			elif etype == "reject":
				r = log_reject(e["plan"], e.get("coil_barcode"), e.get("scanned_qr"),
							   e.get("reason"), e.get("client_event_id"))
			elif etype == "count_scan":
				from sbx_wmslite import count_api
				r = count_api.count_scan(e["bin_code"], e.get("raw_qr") or e.get("coil_barcode"),
										 e.get("client_event_id"), e.get("qc", 0))
			elif etype == "count_remove":
				from sbx_wmslite import count_api
				r = count_api.count_remove(e["bin_code"], e["coil_barcode"], e.get("client_event_id"))
			else:
				r = {"ok": False, "error": f"unknown event type {etype}"}
			results.append({"client_event_id": e.get("client_event_id"), **r})
		except Exception as ex:
			results.append({"client_event_id": e.get("client_event_id"), "ok": False,
							"error": str(ex)})
	return results
