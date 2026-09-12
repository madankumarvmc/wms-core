"""Back-office console endpoints for SBX WMSLite (Loading Supervisor).

Dashboard KPIs, truck drill-down, the inbound SAP log, and a guided upload
fallback for loading plans (parse -> preview -> commit background job ->
rollback). All endpoints require the Loading Supervisor role.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, getdate, now_datetime, today

SUPERVISOR = "Loading Supervisor"
MANAGER = "WMS Manager"
CONSOLE_ROLES = {SUPERVISOR, MANAGER, "System Manager"}
OPEN_STATES = ("Open", "In Progress", "Pending Approval")


def _require_console():
	if not (set(frappe.get_roles()) & CONSOLE_ROLES):
		raise frappe.PermissionError("Loading Supervisor role required")


def _date_range(date_from, date_to):
	"""Build a Frappe filter value spanning whole days [from 00:00:00 .. to 23:59:59].
	Returns None when neither bound is given (no date filter)."""
	if date_from and date_to:
		return ["between", [date_from + " 00:00:00", date_to + " 23:59:59"]]
	if date_from:
		return [">=", date_from + " 00:00:00"]
	if date_to:
		return ["<=", date_to + " 23:59:59"]
	return None


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------
@frappe.whitelist()
def get_dashboard(date_from=None, date_to=None):
	"""Live-state tiles are always "now"; throughput tiles + overall stock are
	scoped to [date_from .. date_to] (whole days). Range only touches throughput."""
	_require_console()

	# --- live state (always current; a stale open truck must never hide) ---
	def n_status(s):
		return frappe.db.count("Loading Plan", {"plan_status": s})

	live = {
		"open": sum(n_status(s) for s in OPEN_STATES),
		"in_progress": n_status("In Progress"),
		"pending_approval": n_status("Pending Approval"),
		"gi_awaiting_release": frappe.db.count("WMSLite SAP Confirmation", {"status": "Held"}),
		"gi_failed": frappe.db.count("WMSLite SAP Confirmation",
			{"status": ["in", ("Failed (Retryable)", "Failed (Business)", "Exhausted")]}),
	}

	# --- throughput (scoped to range) ---
	rng = _date_range(date_from, date_to)
	completed_f = {"plan_status": ["in", ("Completed", "Completed (Short)")]}
	loaded_f = {"event_type": "Loaded"}
	reject_f = {"event_type": "Rejected"}
	if rng:
		completed_f["completed_at"] = rng
		loaded_f["event_time"] = rng
		reject_f["event_time"] = rng

	throughput = {
		"completed": frappe.db.count("Loading Plan", completed_f),
		"coils_loaded": frappe.db.count("Coil Load Event", loaded_f),
		"rejects": frappe.db.count("Coil Load Event", reject_f),
		"date_from": date_from,
		"date_to": date_to,
	}

	# --- overall coil stock for plans received in range (progress bar) ---
	plan_filter = {}
	if rng:
		plan_filter["received_at"] = rng
	rows = frappe.db.get_all("Loading Plan", filters=plan_filter,
		fields=["total_coils", "loaded_coils", "skipped_coils"], limit_page_length=0)
	planned = sum(int(r.total_coils or 0) for r in rows)
	loaded = sum(int(r.loaded_coils or 0) for r in rows)
	skipped = sum(int(r.skipped_coils or 0) for r in rows)

	# --- inventory snapshot (always "now": stock on hand is a live figure, not
	# a throughput one, so the date range must not touch it) ---
	inv = frappe.db.sql("""SELECT COUNT(*) coils, COUNT(DISTINCT bin_code) bins,
			COALESCE(SUM(weight), 0) weight,
			SUM(CASE WHEN status = 'Picked' THEN 1 ELSE 0 END) picked
		FROM `tabBin Inventory`""", as_dict=True)[0]

	return {
		"live": live,
		"throughput": throughput,
		"coils": {
			"planned": planned, "loaded": loaded, "skipped": skipped,
			"pending": planned - loaded - skipped,
			"progress_pct": round(100 * loaded / planned, 1) if planned else 0,
		},
		"inventory": {
			"coils": cint(inv.coils), "bins": cint(inv.bins),
			"weight": round(float(inv.weight or 0), 1), "picked": cint(inv.picked),
		},
		"scans": _scan_stats(date_from, date_to),
	}


@frappe.whitelist()
def get_trucks(status=None, q=None, date_from=None, date_to=None, limit=100):
	_require_console()
	filters = {}
	if status:
		filters["plan_status"] = status
	if q:
		filters["truck_number"] = ["like", f"%{q}%"]
	rng = _date_range(date_from, date_to)
	if rng:
		filters["received_at"] = rng
	return frappe.get_all(
		"Loading Plan", filters=filters,
		fields=["name", "truck_number", "plan_status", "source", "sap_plan_id",
				"total_coils", "loaded_coils", "skipped_coils", "pending_coils",
				"claimed_by", "received_at", "gi_status"],
		order_by="received_at desc", limit_page_length=cint(limit),
	)


@frappe.whitelist()
def get_truck_detail(plan):
	_require_console()
	doc = frappe.get_doc("Loading Plan", plan)
	events = frappe.get_all(
		"Coil Load Event", filters={"loading_plan": plan},
		fields=["event_type", "result", "coil_barcode", "operator", "approver",
				"reason", "event_time", "synced_offline"],
		order_by="event_time desc", limit_page_length=200,
	)
	return {
		"plan": {
			"name": doc.name, "truck_number": doc.truck_number, "plan_status": doc.plan_status,
			"source": doc.source, "sap_plan_id": doc.sap_plan_id,
			"total_coils": doc.total_coils, "loaded_coils": doc.loaded_coils,
			"skipped_coils": doc.skipped_coils, "pending_coils": doc.pending_coils,
			"claimed_by": doc.claimed_by, "approved_by": doc.approved_by,
			"received_at": str(doc.received_at) if doc.received_at else None,
		},
		"coils": [
			{"name": c.name, "coil_barcode": c.coil_barcode, "material_grade": c.material_grade,
			 "weight": c.weight, "coil_status": c.coil_status, "skip_reason": c.skip_reason,
			 "unplanned": cint(c.unplanned), "loaded_by": c.loaded_by,
			 "loaded_at": str(c.loaded_at) if c.loaded_at else None,
			 "sap_loading_status": c.sap_loading_status,
			 "sap_loading_message": c.sap_loading_message,
			 "sap_loading_pushed_at": str(c.sap_loading_pushed_at) if c.sap_loading_pushed_at else None}
			for c in doc.coils
		],
		"events": events,
	}


@frappe.whitelist()
def cancel_plan(plan, reopen=0):
	"""Supervisor cancel / reopen a plan from the console."""
	_require_console()
	doc = frappe.get_doc("Loading Plan", plan)
	if cint(reopen):
		if doc.gi_status == "Confirmed":
			frappe.throw(_(
				"Goods issue already posted to SAP (doc {0}). Reopening needs a "
				"SAP-side reversal first."
			).format(doc.gi_sap_document or "?"))
		doc.plan_status = "Open"
		doc.recompute_counts()  # settles to Open/In Progress based on coils
		action, reason = "Cancel", "Reopened by supervisor"
	else:
		doc.plan_status = "Cancelled"
		action, reason = "Cancel", "Cancelled by supervisor"
	doc.save(ignore_permissions=True)
	frappe.get_doc({"doctype": "Coil Load Event", "loading_plan": doc.name,
					"truck_number": doc.truck_number, "event_type": "Cancel",
					"result": "Success", "operator": frappe.session.user,
					"event_time": now_datetime(), "reason": reason}).insert(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "plan_status": doc.plan_status}


@frappe.whitelist()
def complete_plan(plan):
	"""Supervisor completes a fully-loaded plan from the console — for a truck the
	operator left parked at 'Ready to Complete' (auto-complete off). Reuses the
	operator endpoint so completion behaviour stays single-sourced (fires the SAP
	goods-issue and releases the Deferred T1 pushes)."""
	_require_console()
	from sbx_wmslite import api as operator_api

	return operator_api.complete_plan(plan)


# --------------------------------------------------------------------------
# outbound SAP goods-issue confirmations
# --------------------------------------------------------------------------
@frappe.whitelist()
def get_confirmations(status=None, date_from=None, date_to=None, limit=100):
	_require_console()
	filters = {}
	if status:
		filters["status"] = status
	rng = _date_range(date_from, date_to)
	if rng:
		filters["creation"] = rng
	return frappe.get_all(
		"WMSLite SAP Confirmation", filters=filters,
		fields=["name", "loading_plan", "truck_number", "sap_plan_id", "confirmation_type",
				"status", "attempts", "next_attempt_at", "sap_gi_document", "last_error",
				"http_status", "released_by", "confirmed_at"],
		order_by="modified desc", limit_page_length=cint(limit),
	)


@frappe.whitelist()
def get_confirmation_detail(name):
	_require_console()
	doc = frappe.get_doc("WMSLite SAP Confirmation", name)
	return doc.as_dict()


@frappe.whitelist()
def release_confirmation(name):
	_require_console()
	from sbx_wmslite import outbound_sap

	return outbound_sap.release(name)


@frappe.whitelist()
def retry_confirmation(name):
	_require_console()
	from sbx_wmslite import outbound_sap

	return outbound_sap.manual_retry(name)


@frappe.whitelist()
def get_sap_log(result=None, direction=None, date_from=None, date_to=None, limit=100):
	_require_console()
	filters = {}
	if result:
		filters["result"] = result
	if direction:
		filters["direction"] = direction
	rng = _date_range(date_from, date_to)
	if rng:
		filters["received_at"] = rng
	return frappe.get_all(
		"WMSLite SAP Log", filters=filters,
		fields=["name", "received_at", "direction", "action", "result", "truck_number",
				"sap_plan_id", "loading_plan", "coil_count", "message", "source_ip"],
		order_by="received_at desc", limit_page_length=cint(limit),
	)


# --------------------------------------------------------------------------
# SAP Loading (outbound T1) — per-coil push status
# --------------------------------------------------------------------------
_T1_STATES = ("Pending", "Sent", "Failed")


@frappe.whitelist()
def get_coil_loading(status=None, truck=None, date_from=None, date_to=None, limit=200):
	"""Per-coil T1 loading-push records across all plans. Only coils that are (or
	should be) pushed to SAP appear — i.e. sap_loading_status in Pending/Sent/Failed."""
	_require_console()
	conds = ["c.sap_loading_status IN %(states)s"]
	params = {"states": (status,) if status in _T1_STATES else _T1_STATES,
			  "limit": cint(limit)}
	if truck:
		conds.append("p.truck_number LIKE %(truck)s")
		params["truck"] = "%" + truck + "%"
	if date_from:
		conds.append("c.sap_loading_pushed_at >= %(df)s")
		params["df"] = date_from + " 00:00:00"
	if date_to:
		conds.append("c.sap_loading_pushed_at <= %(dt)s")
		params["dt"] = date_to + " 23:59:59"
	return frappe.db.sql(
		"""
		SELECT c.name, c.coil_barcode, c.parent AS plan, p.truck_number, p.sap_plan_id,
			   c.coil_status, c.weight, c.material_grade,
			   c.sap_loading_status, c.sap_loading_message, c.sap_loading_pushed_at
		FROM `tabLoading Plan Coil` c
		JOIN `tabLoading Plan` p ON p.name = c.parent
		WHERE {where}
		ORDER BY c.sap_loading_pushed_at DESC, c.modified DESC
		LIMIT %(limit)s
		""".format(where=" AND ".join(conds)),
		params, as_dict=True,
	)


@frappe.whitelist()
def retry_coil_loading(coil):
	"""Re-arm a Failed coil's T1 push and enqueue the sweep."""
	_require_console()
	from sbx_wmslite import loading_push

	res = loading_push.requeue(coil)
	if not res.get("ok"):
		frappe.throw(_("Cannot retry: {0}").format(res.get("skipped") or "not retryable"))
	return res


# --------------------------------------------------------------------------
# Bin Inventory — display
# --------------------------------------------------------------------------
@frappe.whitelist()
def get_bin_inventory(q=None, bin_code=None, status=None, limit=50, start=0):
	"""Coil-level inventory, paginated.

	`total` is the count MATCHING the filters (not the table size) — the UI shows
	"showing N of M", so a filtered search must narrow M or the pager lies.
	"""
	_require_console()
	filters = {}
	if bin_code:
		filters["bin_code"] = bin_code
	if status:
		filters["status"] = status
	or_filters = None
	if q:
		or_filters = [["coil_barcode", "like", f"{q}%"], ["bin_code", "like", f"{q}%"]]
	common = {"filters": filters, "or_filters": or_filters}
	rows = frappe.get_all(
		"Bin Inventory",
		fields=["name as coil_barcode", "bin_code", "material_grade", "weight", "zone",
				"aisle", "status", "source", "updated_at"],
		# most-recently-touched first: with 50k+ coils the useful rows are the
		# ones that just moved, and bin_code asc pinned you to the same page.
		order_by="updated_at desc, coil_barcode asc",
		limit_page_length=cint(limit), limit_start=cint(start), **common)
	total = frappe.db.count("Bin Inventory", filters=filters) if not q else len(
		frappe.get_all("Bin Inventory", pluck="name", limit_page_length=0, **common))
	return {"rows": rows, "total": total, "start": cint(start), "limit": cint(limit)}


@frappe.whitelist()
def get_bin_rollup(q=None, limit=50, start=0):
	"""Inventory grouped by bin — 760 bins instead of 53k coils.

	This is the default Inventory view: a flat coil list of that size can't be
	browsed, but the bin count is small enough to page through, and each row
	drills into its coils via get_bin_inventory(bin_code=...).
	"""
	_require_console()
	where, vals = ["1 = 1"], {}
	if q:
		where.append("(bin_code LIKE %(q)s OR coil_barcode LIKE %(q)s)")
		vals["q"] = f"{q}%"   # prefix: index-backed (see _txn_where)
	clause = " AND ".join(where)
	rows = frappe.db.sql(f"""SELECT bin_code, COUNT(*) coils, COALESCE(SUM(weight), 0) weight,
			MAX(updated_at) last_updated,
			SUM(CASE WHEN status = 'Picked' THEN 1 ELSE 0 END) picked
		FROM `tabBin Inventory` WHERE {clause}
		GROUP BY bin_code ORDER BY bin_code ASC
		LIMIT %(limit)s OFFSET %(start)s""",
		dict(vals, limit=cint(limit), start=cint(start)), as_dict=True)
	total = frappe.db.sql(
		f"SELECT COUNT(DISTINCT bin_code) FROM `tabBin Inventory` WHERE {clause}", vals)[0][0]
	return {"rows": rows, "total": cint(total), "start": cint(start), "limit": cint(limit)}


# --------------------------------------------------------------------------
# scan transactions (Coil Transaction — the append-only scan feed)
# --------------------------------------------------------------------------
def _txn_where(date_from=None, date_to=None, source=None, q=None, unprocessed=0):
	"""Shared WHERE builder for the transaction list + its counts."""
	where, vals = ["1 = 1"], {}
	if date_from:
		where.append("scanned_at >= %(df)s")
		vals["df"] = f"{date_from} 00:00:00"
	if date_to:
		where.append("scanned_at <= %(dt)s")
		vals["dt"] = f"{date_to} 23:59:59"
	if source:
		where.append("source = %(src)s")
		vals["src"] = source
	if cint(unprocessed):
		where.append("processed = 0")
	if q:
		# Prefix-anchored on purpose: a leading wildcard makes this a full scan of
		# 250k+ rows (EXPLAIN: type=ALL, no key), while "q%" uses the coil_barcode
		# index. Barcodes and bin codes are also naturally typed from the start.
		where.append("(coil_barcode LIKE %(q)s OR bin_code LIKE %(q)s)")
		vals["q"] = f"{q}%"
	return " AND ".join(where), vals


def _scan_stats(date_from=None, date_to=None):
	"""Dashboard tiles for scan activity, scoped to the same range as throughput.

	The DISTINCT is deliberately a SEPARATE query: folded into the aggregate below
	it forces a full table scan (measured 2807ms over 260k rows), while on its own
	it rides the coil_barcode index (503ms).
	"""
	clause, vals = _txn_where(date_from, date_to)
	row = frappe.db.sql(f"""SELECT COUNT(*) total,
			SUM(CASE WHEN source = 'Push API' THEN 1 ELSE 0 END) sap_feed,
			SUM(CASE WHEN source = 'In-app' THEN 1 ELSE 0 END) in_app,
			MAX(scanned_at) last_scan
		FROM `tabCoil Transaction` WHERE {clause}""", vals, as_dict=True)[0]
	coils = frappe.db.sql(
		f"SELECT COUNT(DISTINCT coil_barcode) FROM `tabCoil Transaction` WHERE {clause}", vals)[0][0]
	return {
		"total": cint(row.total), "coils": cint(coils),
		"sap_feed": cint(row.sap_feed), "in_app": cint(row.in_app),
		"last_scan": str(row.last_scan) if row.last_scan else None,
		# never range-scoped: a derivation backlog is a "right now" problem
		"unprocessed": frappe.db.count("Coil Transaction", {"processed": 0}),
	}


@frappe.whitelist()
def get_coil_transactions(q=None, source=None, date_from=None, date_to=None,
						  unprocessed=0, limit=50, start=0):
	"""The scan feed, newest first, paginated.

	`source` splits the two very different populations sharing this table:
	'Push API' (the SAP inventory feed) vs 'In-app' (Inventory Count scans).
	"""
	_require_console()
	clause, vals = _txn_where(date_from, date_to, source, q, unprocessed)
	rows = frappe.db.sql(f"""SELECT name, coil_barcode, bin_code, scanned_at, transaction_type,
			source, username, weight, material_grade, qc, processed, plant, product
		FROM `tabCoil Transaction` WHERE {clause}
		ORDER BY scanned_at DESC, name DESC
		LIMIT %(limit)s OFFSET %(start)s""",
		dict(vals, limit=cint(limit), start=cint(start)), as_dict=True)
	total = frappe.db.sql(f"SELECT COUNT(*) FROM `tabCoil Transaction` WHERE {clause}", vals)[0][0]
	return {"rows": rows, "total": cint(total), "start": cint(start), "limit": cint(limit),
			"stats": _scan_stats(date_from, date_to)}


@frappe.whitelist()
def get_transaction_detail(name):
	"""One scan with its raw payload — the debugging seam for the SAP feed."""
	_require_console()
	d = frappe.db.get_value(
		"Coil Transaction", name,
		["name", "coil_barcode", "bin_code", "scanned_at", "transaction_type", "source",
		 "username", "weight", "material_grade", "qc", "plant", "product", "sized",
		 "processed", "processed_at", "bin_inventory", "txn_key", "raw_json", "creation"],
		as_dict=True)
	if not d:
		frappe.throw(_("Transaction {0} not found").format(name))
	return d


@frappe.whitelist()
def reprocess_transactions():
	"""Derive Bin Inventory from any still-unprocessed scans, now.

	The `*/5` scheduler sweep normally does this; this button exists so a
	supervisor isn't blocked when the scheduler is down.
	"""
	_require_console()
	from sbx_wmslite import bin_api

	pending = frappe.db.count("Coil Transaction", {"processed": 0})
	bin_api.process_pending_transactions()
	frappe.db.commit()
	return {"ok": True, "processed": pending,
			"remaining": frappe.db.count("Coil Transaction", {"processed": 0})}


# --------------------------------------------------------------------------
# Excel export — every console view streams the rows behind what's on screen
# --------------------------------------------------------------------------
# The console pages 50 rows at a time; an export is only useful if it carries the
# whole filtered set, so it re-runs the same query with a much larger ceiling.
# The cap is a memory guard (an unfiltered Coil Transaction export would be 260k+
# rows): when it bites, the sheet says so rather than silently ending short.
EXPORT_LIMIT = 20000

# view -> (sheet name, [(column label, row key), ...])
_EXPORT_COLUMNS = {
	"trucks": ("Trucks", [
		("Plan", "name"), ("Truck", "truck_number"), ("Status", "plan_status"),
		("GI Status", "gi_status"), ("Source", "source"), ("SAP Plan ID", "sap_plan_id"),
		("Total Coils", "total_coils"), ("Loaded", "loaded_coils"),
		("Skipped", "skipped_coils"), ("Pending", "pending_coils"),
		("Claimed By", "claimed_by"), ("Received At", "received_at"),
	]),
	"inventory_bin": ("Inventory by Bin", [
		("Bin", "bin_code"), ("Coils", "coils"), ("Weight", "weight"),
		("Picked", "picked"), ("Last Updated", "last_updated"),
	]),
	"inventory_coil": ("Inventory by Coil", [
		("Bin", "bin_code"), ("Coil", "coil_barcode"), ("Grade", "material_grade"),
		("Weight", "weight"), ("Zone", "zone"), ("Aisle", "aisle"),
		("Status", "status"), ("Source", "source"), ("Updated", "updated_at"),
	]),
	"transactions": ("Scan Transactions", [
		("Scanned At", "scanned_at"), ("Coil", "coil_barcode"), ("Bin", "bin_code"),
		("Type", "transaction_type"), ("Source", "source"), ("Scanned By", "username"),
		("Weight", "weight"), ("Grade", "material_grade"), ("QC", "qc"),
		("Plant", "plant"), ("Product", "product"), ("Derived", "processed"),
	]),
	"confirmations": ("SAP Confirmations", [
		("Confirmation", "name"), ("Truck", "truck_number"), ("Loading Plan", "loading_plan"),
		("SAP Plan ID", "sap_plan_id"), ("Type", "confirmation_type"), ("Status", "status"),
		("Attempts", "attempts"), ("GI Document", "sap_gi_document"),
		("HTTP Status", "http_status"), ("Last Error", "last_error"),
		("Released By", "released_by"), ("Confirmed At", "confirmed_at"),
		("Next Attempt", "next_attempt_at"),
	]),
	"saploading": ("SAP Loading", [
		("Coil", "coil_barcode"), ("Truck", "truck_number"), ("Plan", "plan"),
		("SAP Plan ID", "sap_plan_id"), ("Coil Status", "coil_status"),
		("Grade", "material_grade"), ("Weight", "weight"),
		("T1 Status", "sap_loading_status"), ("Pushed At", "sap_loading_pushed_at"),
		("Message", "sap_loading_message"),
	]),
	"saplog": ("SAP Log", [
		("Time", "received_at"), ("Direction", "direction"), ("Action", "action"),
		("Result", "result"), ("Truck", "truck_number"), ("SAP Plan ID", "sap_plan_id"),
		("Loading Plan", "loading_plan"), ("Coils", "coil_count"),
		("Message", "message"), ("Source IP", "source_ip"),
	]),
	"rm": ("RM Movement", [
		("Coil", "coil_barcode"), ("State", "rm_status"), ("Line", "rm_destination"),
		("From Bin", "rm_source_bin"), ("Trip", "rm_cycle"),
		("Hours In Transit", "age_hours"), ("Flagged", "rm_unplanned"),
		("Expected At", "rm_expected_destination"),
		("Dispatched By", "rm_dispatched_by"), ("Dispatched At", "rm_dispatched_at"),
		("Received By", "rm_received_by"), ("Received At", "rm_received_at"),
		("Grade", "material_grade"), ("Weight", "weight"),
	]),
	"truck_detail": ("Truck Coils", [
		("Coil", "coil_barcode"), ("Status", "coil_status"), ("Grade", "material_grade"),
		("Weight", "weight"), ("Unplanned", "unplanned"), ("Skip Reason", "skip_reason"),
		("Loaded By", "loaded_by"), ("Loaded At", "loaded_at"),
		("T1 Status", "sap_loading_status"), ("Pushed At", "sap_loading_pushed_at"),
		("Message", "sap_loading_message"),
	]),
}


def _export_rows(view, args):
	"""Fetch the full (filtered) row set for a console view.

	Deliberately calls the same endpoints the screen does, so an export can never
	drift from what the supervisor was looking at when they clicked it.
	"""
	limit = EXPORT_LIMIT
	df, dt = args.get("date_from"), args.get("date_to")
	q, status = args.get("q"), args.get("status")

	if view == "trucks":
		return get_trucks(status=status, q=q, date_from=df, date_to=dt, limit=limit)
	if view == "inventory_bin":
		return get_bin_rollup(q=q, limit=limit, start=0)["rows"]
	if view == "inventory_coil":
		return get_bin_inventory(q=q, bin_code=args.get("bin_code"), status=status,
								 limit=limit, start=0)["rows"]
	if view == "transactions":
		return get_coil_transactions(q=q, source=args.get("source"), date_from=df, date_to=dt,
									 unprocessed=args.get("unprocessed") or 0,
									 limit=limit, start=0)["rows"]
	if view == "confirmations":
		return get_confirmations(status=status, date_from=df, date_to=dt, limit=limit)
	if view == "saploading":
		return get_coil_loading(status=status, truck=args.get("truck") or q,
								date_from=df, date_to=dt, limit=limit)
	if view == "saplog":
		return get_sap_log(result=args.get("result"), direction=args.get("direction"),
						   date_from=df, date_to=dt, limit=limit)
	if view == "rm":
		return get_rm_movement(status=status, destination=args.get("destination"),
							   flagged=args.get("flagged") or 0, q=q,
							   date_from=df, date_to=dt, limit=limit, start=0)["rows"]
	if view == "truck_detail":
		plan = args.get("plan")
		if not plan:
			frappe.throw(_("plan is required for this export"))
		return get_truck_detail(plan)["coils"]
	frappe.throw(_("Unknown export view: {0}").format(view))


def _xlsx_response(filename, sheet, header_lines, columns, rows):
	"""Build a one-sheet workbook and hand it to Frappe's download response."""
	from frappe.utils.xlsxutils import make_xlsx

	data = [[line] for line in header_lines]
	data.append([])
	data.append([label for label, _key in columns])
	for r in rows:
		data.append([_cell(r.get(key)) for _label, key in columns])

	xlsx = make_xlsx(data, sheet)
	frappe.response["filename"] = filename
	frappe.response["filecontent"] = xlsx.getvalue()
	frappe.response["type"] = "binary"


def _cell(v):
	"""openpyxl writes dates/numbers natively; everything else goes in as text."""
	import datetime

	if v is None:
		return ""
	if isinstance(v, (int, float, datetime.date, datetime.datetime)):
		return v
	return str(v)


@frappe.whitelist()
def export_view(view, **kwargs):
	"""Download the current console view as .xlsx, honouring its live filters.

	Called as a plain GET link from the console so the browser handles the save
	dialog; every list view passes the exact filter args it is displaying.
	"""
	_require_console()
	if view not in _EXPORT_COLUMNS:
		frappe.throw(_("Unknown export view: {0}").format(view))
	sheet, columns = _EXPORT_COLUMNS[view]
	rows = _export_rows(view, kwargs)

	stamp = now_datetime()
	head = ["SBX WMSLite — " + sheet,
			"Exported {0} by {1}".format(stamp.strftime("%Y-%m-%d %H:%M"), frappe.session.user)]
	head.append("Filters: " + _filter_note(view, kwargs))
	if len(rows) >= EXPORT_LIMIT:
		head.append("NOTE: truncated at the {0}-row export limit — narrow the filters "
					"for the rest.".format(EXPORT_LIMIT))

	_xlsx_response(
		"wmslite_{0}_{1}.xlsx".format(view, stamp.strftime("%Y%m%d_%H%M%S")),
		sheet, head, columns, rows)


_FILTER_LABELS = {"date_from": "from", "date_to": "to", "q": "search", "status": "status",
				  "source": "source", "direction": "direction", "result": "result",
				  "truck": "truck", "bin_code": "bin", "plan": "plan",
				  "unprocessed": "unprocessed only",
				  "destination": "line", "flagged": "flagged only"}

# Only the keys a view actually queries on: echoing an ignored param (a date on
# the inventory rollup, say) would read as a filter that never ran.
_VIEW_FILTERS = {
	"trucks": ["q", "status", "date_from", "date_to"],
	"inventory_bin": ["q"],
	"inventory_coil": ["q", "bin_code", "status"],
	"transactions": ["q", "source", "unprocessed", "date_from", "date_to"],
	"confirmations": ["status", "date_from", "date_to"],
	"saploading": ["status", "truck", "q", "date_from", "date_to"],
	"saplog": ["result", "direction", "date_from", "date_to"],
	"rm": ["q", "status", "destination", "flagged", "date_from", "date_to"],
	"truck_detail": ["plan"],
}


def _filter_note(view, args):
	"""Human-readable filter line for the sheet header — an exported file that
	doesn't say what it was filtered by gets misread once it leaves the console."""
	parts = []
	for key in _VIEW_FILTERS.get(view, []):
		val = args.get(key)
		if val in (None, "", 0, "0"):
			continue
		label = _FILTER_LABELS[key]
		# Boolean-style filters read as their own label; the rest as "label=value".
		parts.append(label if key in ("unprocessed", "flagged")
					 else "{0}={1}".format(label, val))
	return ", ".join(parts) or "none (all rows)"


@frappe.whitelist()
def export_dashboard(date_from=None, date_to=None):
	"""The dashboard is tiles, not a table — export it as metric/value pairs."""
	_require_console()
	d = get_dashboard(date_from, date_to)
	rng = "{0} → {1}".format(date_from or "beginning", date_to or "today")
	rows = [
		("Now", "Open / Active trucks", d["live"]["open"]),
		("Now", "In Progress", d["live"]["in_progress"]),
		("Now", "Awaiting Approval", d["live"]["pending_approval"]),
		("Now", "GI Awaiting Release", d["live"]["gi_awaiting_release"]),
		("Now", "GI Failed", d["live"]["gi_failed"]),
		("Throughput", "Completed trucks", d["throughput"]["completed"]),
		("Throughput", "Coils loaded", d["throughput"]["coils_loaded"]),
		("Throughput", "Rejects", d["throughput"]["rejects"]),
		("Coil progress", "Planned", d["coils"]["planned"]),
		("Coil progress", "Loaded", d["coils"]["loaded"]),
		("Coil progress", "Skipped", d["coils"]["skipped"]),
		("Coil progress", "Pending", d["coils"]["pending"]),
		("Coil progress", "Progress %", d["coils"]["progress_pct"]),
		("Inventory on hand", "Coils in stock", d["inventory"]["coils"]),
		("Inventory on hand", "Bins occupied", d["inventory"]["bins"]),
		("Inventory on hand", "Total weight", d["inventory"]["weight"]),
		("Inventory on hand", "Picked", d["inventory"]["picked"]),
		("Scan activity", "Scans", d["scans"]["total"]),
		("Scan activity", "From SAP feed", d["scans"]["sap_feed"]),
		("Scan activity", "Inventory count", d["scans"]["in_app"]),
		("Scan activity", "Coils touched", d["scans"]["coils"]),
		("Scan activity", "Unprocessed (now)", d["scans"]["unprocessed"]),
		("Scan activity", "Last scan", d["scans"]["last_scan"] or "—"),
	]
	stamp = now_datetime()
	_xlsx_response(
		"wmslite_dashboard_{0}.xlsx".format(stamp.strftime("%Y%m%d_%H%M%S")),
		"Dashboard",
		["SBX WMSLite — Dashboard",
		 "Exported {0} by {1}".format(stamp.strftime("%Y-%m-%d %H:%M"), frappe.session.user),
		 "Throughput + scan range: " + rng,
		 "Live and inventory figures are as of the export time, not the range."],
		[("Section", "section"), ("Metric", "metric"), ("Value", "value")],
		[{"section": s, "metric": m, "value": v} for s, m, v in rows])


# --------------------------------------------------------------------------
# upload fallback: parse -> preview -> commit -> rollback (Loading Plan + Bin Inventory)
# --------------------------------------------------------------------------
DATASETS = {
	"Loading Plan": {
		"columns": ["sap_plan_id", "truck_number", "coil_barcode", "material_grade", "weight"],
		"required": ["truck_number", "coil_barcode"],
		"target": "Loading Plan",
		"template_rows": [["MH12AB1234", "COIL000001", "LP2026-00045", "GR50", "5200"],
						  ["MH12AB1234", "COIL000002", "LP2026-00045", "GR50", "4800"]],
		"template_headers": ["truck_number", "coil_barcode", "sap_plan_id", "material_grade", "weight"],
	},
	"Bin Inventory": {
		"columns": ["coil_barcode", "bin_code", "material_grade", "weight", "zone", "aisle"],
		"required": ["coil_barcode", "bin_code"],
		"target": "Bin Inventory",
		"template_rows": [["COIL000001", "A-12-03", "GR50", "5200", "A", "12"],
						  ["COIL000002", "A-12-04", "GR50", "4800", "A", "12"]],
		"template_headers": ["coil_barcode", "bin_code", "material_grade", "weight", "zone", "aisle"],
	},
}


def _dataset(name):
	if name not in DATASETS:
		frappe.throw(_("Unknown dataset: {0}").format(name))
	return DATASETS[name]


def _read_rows(file_url):
	from frappe.utils.file_manager import get_file
	from frappe.utils.xlsxutils import read_xlsx_file_from_attached_file

	fname, content = None, None
	fdoc = frappe.get_doc("File", {"file_url": file_url})
	path = fdoc.get_full_path()
	if file_url.lower().endswith(".xlsx"):
		rows = read_xlsx_file_from_attached_file(fcontent=fdoc.get_content())
	else:
		import csv
		import io

		text = fdoc.get_content()
		if isinstance(text, bytes):
			text = text.decode("utf-8-sig")
		rows = list(csv.reader(io.StringIO(text)))
	return rows


@frappe.whitelist()
def get_import_template(dataset="Loading Plan"):
	"""Stream a blank CSV template for the chosen dataset with example rows."""
	_require_console()
	import csv
	import io

	spec = _dataset(dataset)
	buf = io.StringIO()
	w = csv.writer(buf)
	w.writerow(spec["template_headers"])
	for r in spec["template_rows"]:
		w.writerow(r)
	frappe.response["filename"] = dataset.lower().replace(" ", "_") + "_template.csv"
	frappe.response["filecontent"] = buf.getvalue()
	frappe.response["type"] = "download"


@frappe.whitelist()
def parse_upload(file_url, dataset="Loading Plan"):
	_require_console()
	spec = _dataset(dataset)
	rows = _read_rows(file_url)
	if not rows:
		frappe.throw(_("File is empty"))
	headers = [str(h).strip() for h in rows[0]]
	suggested = {}
	for col in spec["columns"]:
		for h in headers:
			if h.lower().replace(" ", "_") == col:
				suggested[col] = h
				break
	return {"headers": headers, "sample": rows[1:6], "columns": spec["columns"],
			"required": spec["required"], "suggested_mapping": suggested, "total_rows": len(rows) - 1}


def _mapped_rows(file_url, mapping):
	rows = _read_rows(file_url)
	headers = [str(h).strip() for h in rows[0]]
	idx = {col: headers.index(src) for col, src in mapping.items() if src in headers}
	out = []
	for r in rows[1:]:
		if not any(str(x).strip() for x in r):
			continue
		out.append({col: (str(r[i]).strip() if i < len(r) else "") for col, i in idx.items()})
	return out


@frappe.whitelist()
def import_preview(file_url, mapping, dataset="Loading Plan"):
	_require_console()
	spec = _dataset(dataset)
	mapping = json.loads(mapping) if isinstance(mapping, str) else mapping
	rows = _mapped_rows(file_url, mapping)
	errors = []
	for n, row in enumerate(rows, start=2):
		for req in spec["required"]:
			if not row.get(req):
				errors.append(f"Row {n}: missing {req}")
	if dataset == "Bin Inventory":
		groups = len({r.get("bin_code") for r in rows if r.get("bin_code")})
		unit = "bins"
	else:
		groups = len({(r.get("sap_plan_id") or ("TRUCK:" + r.get("truck_number", ""))) for r in rows})
		unit = "plans"
	return {"total_rows": len(rows), "plans": groups, "group_unit": unit,
			"errors": len(errors), "sample_errors": errors[:20]}


@frappe.whitelist()
def import_commit(file_url, mapping, label=None, file_name=None, dataset="Loading Plan"):
	_require_console()
	spec = _dataset(dataset)
	mapping = json.loads(mapping) if isinstance(mapping, str) else mapping
	batch = frappe.get_doc({
		"doctype": "Import Batch", "batch_label": label or (dataset + " upload"),
		"dataset": dataset, "target_doctype": spec["target"], "status": "Queued",
		"file_url": file_url, "file_name": file_name, "import_time": now_datetime(),
		"imported_by": frappe.session.user,
	}).insert(ignore_permissions=True)
	frappe.db.commit()
	frappe.enqueue("sbx_wmslite.console_api._run_import", queue="short", timeout=1500,
				   batch=batch.name, file_url=file_url, mapping=mapping, dataset=dataset)
	return {"batch": batch.name}


def _run_import(batch, file_url, mapping, dataset="Loading Plan"):
	b = frappe.get_doc("Import Batch", batch)
	b.status = "Importing"
	b.save(ignore_permissions=True)
	frappe.db.commit()
	rows = _mapped_rows(file_url, mapping)

	if dataset == "Bin Inventory":
		inserted, errors, errlog = _run_bin_import(rows)
		total = len(rows)
	else:
		inserted, errors, errlog, total = _run_plan_import(rows, batch)

	b.reload()
	b.total_rows = total
	b.inserted = inserted
	b.errors = errors
	b.error_report = "\n".join(errlog)
	b.status = "Completed" if errors == 0 else ("Partial" if inserted else "Failed")
	b.save(ignore_permissions=True)
	frappe.db.commit()


def _run_plan_import(rows, batch):
	from sbx_wmslite import sap_api

	plans = {}
	for row in rows:
		key = row.get("sap_plan_id") or ("TRUCK:" + row.get("truck_number", ""))
		plans.setdefault(key, []).append(row)
	inserted, errors, errlog = 0, 0, []
	for key, prows in plans.items():
		truck = prows[0].get("truck_number")
		sap_plan_id = prows[0].get("sap_plan_id") or None
		coils = [{"coil_barcode": r.get("coil_barcode"), "material_grade": r.get("material_grade"),
				  "weight": r.get("weight") or None} for r in prows if r.get("coil_barcode")]
		try:
			existing = frappe.db.get_value("Loading Plan", {"sap_plan_id": sap_plan_id}, "name") if sap_plan_id else None
			r = sap_api._upsert(existing, truck, sap_plan_id, coils, {"upload_batch": batch})
			if r.get("ok"):
				frappe.db.set_value("Loading Plan", r["plan"], "source", "Upload")
				frappe.db.set_value("Loading Plan", r["plan"], "import_batch", batch)
				inserted += 1
			elif r.get("duplicate"):
				errors += 1
				errlog.append(
					f"{truck}: sap_plan_id '{sap_plan_id}' already exists on {r.get('plan')} "
					f"— use a new sap_plan_id or leave it blank")
			else:
				errors += 1
				errlog.append(f"{truck}: {r.get('error')}")
		except Exception as e:
			errors += 1
			errlog.append(f"{truck}: {e}")
	return inserted, errors, errlog, len(plans)


def _run_bin_import(rows):
	from sbx_wmslite import bin_api

	inserted, errors, errlog = 0, 0, []
	for row in rows:
		code = (row.get("coil_barcode") or "").strip()
		if not code or not (row.get("bin_code") or "").strip():
			errors += 1
			errlog.append(f"{code or '(blank)'}: missing coil_barcode or bin_code")
			continue
		try:
			bin_api.upsert_bin(code, row, source="Upload")
			inserted += 1
		except Exception as e:
			errors += 1
			errlog.append(f"{code}: {e}")
	return inserted, errors, errlog


@frappe.whitelist()
def get_import_status(batch):
	_require_console()
	b = frappe.get_doc("Import Batch", batch)
	return {"status": b.status, "total_rows": b.total_rows, "inserted": b.inserted,
			"errors": b.errors, "error_report": b.error_report,
			"done": b.status in ("Completed", "Partial", "Failed", "Rolled Back")}


@frappe.whitelist()
def delete_import_batch(batch):
	"""Roll back an upload: delete the plans it created, then the batch."""
	_require_console()
	for name in frappe.get_all("Loading Plan", filters={"import_batch": batch}, pluck="name"):
		frappe.delete_doc("Loading Plan", name, force=1, ignore_permissions=True)
	frappe.db.set_value("Import Batch", batch, "status", "Rolled Back")
	frappe.db.commit()
	return {"ok": True}


# --------------------------------------------------------------------------
# RM Movement (gate -> line)
# --------------------------------------------------------------------------
# RM has no header document — a "trip" is just coil state — so these views are the
# only place a supervisor can see what is out on the floor. Two things need
# watching: coils that left the gate and never arrived (ageing in transit), and
# receipts that landed flagged because a replayed offline scan no longer matched
# the expected list. The strict-receipt rule means flagged rows can ONLY come from
# that replay path, so the list stays short and is worth reading.
RM_IN_TRANSIT = "In Transit to Line"
RM_RECEIVED = "Received"


@frappe.whitelist()
def get_rm_summary(stale_hours=4):
	"""Per-line rollup: what is in transit, how long it has been, what is flagged.

	`oldest_hours` is the health signal. A line whose oldest in-transit coil is
	hours old is either mid-shift or has lost a coil, and the supervisor is the only
	one positioned to tell the difference.
	"""
	_require_console()
	stale_hours = cint(stale_hours) or 4
	rows = frappe.db.sql("""
		SELECT rm_destination destination,
			SUM(CASE WHEN rm_status = %(transit)s THEN 1 ELSE 0 END) in_transit,
			SUM(CASE WHEN rm_status = %(transit)s THEN COALESCE(weight, 0) ELSE 0 END) transit_weight,
			SUM(CASE WHEN rm_status = %(recvd)s AND rm_unplanned = 1 THEN 1 ELSE 0 END) flagged,
			MIN(CASE WHEN rm_status = %(transit)s THEN rm_dispatched_at END) oldest_dispatch,
			MAX(CASE WHEN rm_status = %(recvd)s THEN rm_received_at END) last_receipt
		FROM `tabBin Inventory`
		WHERE rm_destination IS NOT NULL AND rm_destination != ''
		GROUP BY rm_destination
		HAVING in_transit > 0 OR flagged > 0
		ORDER BY in_transit DESC, destination ASC""",
		{"transit": RM_IN_TRANSIT, "recvd": RM_RECEIVED}, as_dict=True)

	now = now_datetime()
	for r in rows:
		r["oldest_hours"] = (round((now - get_datetime(r.oldest_dispatch)).total_seconds() / 3600.0, 1)
							 if r.oldest_dispatch else None)
		r["stale"] = 1 if (r["oldest_hours"] is not None and r["oldest_hours"] >= stale_hours) else 0
	return {
		"rows": rows,
		"stale_hours": stale_hours,
		"totals": {
			"in_transit": sum(cint(r.in_transit) for r in rows),
			"flagged": sum(cint(r.flagged) for r in rows),
			"lines": len(rows),
			"stale_lines": sum(r["stale"] for r in rows),
		},
	}


@frappe.whitelist()
def get_rm_movement(status=None, destination=None, flagged=0, q=None,
					date_from=None, date_to=None, limit=200, start=0):
	"""Coil-level RM movement list.

	Dates filter on the timestamp that matters for the row's own state — dispatch
	time for a coil in transit, receipt time for one that has landed. Filtering both
	on one column would make the range mean different things in different rows.
	"""
	_require_console()
	where, vals = ["bi.rm_status IS NOT NULL", "bi.rm_status != ''"], {}
	if status:
		where.append("bi.rm_status = %(status)s")
		vals["status"] = status
	if destination:
		where.append("bi.rm_destination = %(dest)s")
		vals["dest"] = destination
	if cint(flagged):
		where.append("bi.rm_unplanned = 1")
	if q:
		where.append("(bi.coil_barcode LIKE %(q)s OR bi.rm_destination LIKE %(q)s)")
		vals["q"] = f"{q}%"   # prefix keeps this index-backed
	stamp = ("CASE WHEN bi.rm_status = '{0}' THEN bi.rm_received_at "
			 "ELSE bi.rm_dispatched_at END").format(RM_RECEIVED)
	if date_from:
		where.append(f"{stamp} >= %(df)s")
		vals["df"] = date_from + " 00:00:00"
	if date_to:
		where.append(f"{stamp} <= %(dt)s")
		vals["dt"] = date_to + " 23:59:59"
	clause = " AND ".join(where)

	rows = frappe.db.sql(f"""
		SELECT bi.coil_barcode, bi.rm_status, bi.rm_destination, bi.rm_source_bin,
			bi.rm_cycle, bi.rm_unplanned, bi.rm_expected_destination,
			bi.rm_dispatched_by, bi.rm_dispatched_at,
			bi.rm_received_by, bi.rm_received_at,
			bi.material_grade, bi.weight, bi.bin_code
		FROM `tabBin Inventory` bi
		WHERE {clause}
		ORDER BY {stamp} DESC, bi.coil_barcode ASC
		LIMIT %(limit)s OFFSET %(start)s""",
		dict(vals, limit=cint(limit), start=cint(start)), as_dict=True)

	now = now_datetime()
	for r in rows:
		r["age_hours"] = (round((now - get_datetime(r.rm_dispatched_at)).total_seconds() / 3600.0, 1)
						  if r.rm_status == RM_IN_TRANSIT and r.rm_dispatched_at else None)
	total = frappe.db.sql(
		f"SELECT COUNT(*) FROM `tabBin Inventory` bi WHERE {clause}", vals)[0][0]
	return {"rows": rows, "total": cint(total), "start": cint(start), "limit": cint(limit)}


@frappe.whitelist()
def get_rm_coil_history(coil_barcode):
	"""Every RM ledger row for one coil, oldest first — the audit trail.

	Reversed rows are included and marked rather than hidden: "this was undone" is
	the fact a supervisor is usually chasing.
	"""
	_require_console()
	rows = frappe.get_all(
		"Coil Transaction",
		filters={"coil_barcode": (coil_barcode or "").strip(),
				 "transaction_type": ("like", "RM %")},
		fields=["name", "transaction_type", "rm_destination", "rm_cycle", "scanned_at",
				"username", "reversed", "reversed_by", "reversed_at", "raw_json"],
		order_by="scanned_at asc, creation asc", limit_page_length=0)
	return {"coil_barcode": coil_barcode, "rows": rows,
			"state": frappe.db.get_value(
				"Bin Inventory", coil_barcode,
				["rm_status", "rm_destination", "rm_unplanned", "rm_expected_destination"],
				as_dict=True)}


@frappe.whitelist()
def rm_clear_flag(coil_barcode, note=None):
	"""Acknowledge a flagged receipt. Console roles only — never the line operator."""
	_require_console()
	from sbx_wmslite import rm_api

	return rm_api.clear_unplanned_flag(coil_barcode, note=note)
