"""Back-office console endpoints for SBX WMSLite (Loading Supervisor).

Dashboard KPIs, truck drill-down, the inbound SAP log, and a guided upload
fallback for loading plans (parse -> preview -> commit background job ->
rollback). All endpoints require the Loading Supervisor role.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, getdate, now_datetime, today

from sbx_wmslite import classify, locations
from sbx_wmslite.decode import qr_size, qr_weight_mt

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
# global filters (Site / Material Type) + dashboard
# --------------------------------------------------------------------------
# Every list endpoint takes `site` / `material_type` (both default None = no
# filter). The console sends them on every call; Frappe drops the kwarg on any
# endpoint that does not declare it. How each table resolves them is in
# classify.py: Bin Inventory / WMSLite Bin derive at query time from the bin
# prefix, Coil Transaction reads its stored columns, Loading Plans go through
# the SAP plant code. This map tells the console which views honour which.
_VIEW_SUPPORT = {
	"dashboard": ["site", "material_type"],
	"inventory": ["site", "material_type"],
	"transactions": ["site", "material_type"],
	"rm": ["site", "material_type"],
	"trucks": ["site"],
	"binmaster": ["site"],
	"confirmations": ["site"],
	"saploading": ["site"],
	"saplog": [],
	"import": [],
	"users": [],
}

# Dashboard section groups — the "Transaction Type" selector. Only the requested
# groups are computed: the scans group is a multi-second aggregate over the whole
# feed for "all time", and a supervisor looking at loading should not pay for it.
DASH_GROUPS = ("loading", "inventory", "rm")
RM_TXN_DISPATCH = "RM Dispatch"
RM_TXN_RECEIPT = "RM Receipt"


@frappe.whitelist()
def get_filter_options():
	"""What the console's global filter bar offers, and which views honour it."""
	_require_console()
	return {
		"sites": classify.site_options(),
		"material_types": list(classify.MATERIAL_TYPES),
		"txn_types": [list(g) for g in classify.TXN_GROUPS],
		"support": _VIEW_SUPPORT,
	}


def _site_plan_names(site):
	"""Loading Plan names belonging to `site`, or None when no site filter is on.

	Loading Plans carry no bin code, so site comes from `source_plant` through
	the plant codes on the WMSLite Site master. The list is small (139 plans on
	the client's site); switch to a join if that ever changes.
	"""
	if not site:
		return None
	vals = {}
	where = classify.plant_where("source_plant", site, vals)
	return frappe.db.sql_list(f"SELECT name FROM `tabLoading Plan` WHERE {where}", vals)


def _loading_stats(date_from, date_to, site):
	"""Loading (Outbound) tiles: live plan state, throughput, coil progress.
	Live tiles are always "now" (a stale open truck must never hide); throughput
	and progress are scoped to [date_from .. date_to] (whole days)."""
	plan_names = _site_plan_names(site)
	if plan_names == []:   # a site with no plans at all: everything is zero
		return {
			"live": {"open": 0, "in_progress": 0, "pending_approval": 0,
					 "gi_awaiting_release": 0, "gi_failed": 0},
			"throughput": {"completed": 0, "coils_loaded": 0, "rejects": 0,
						   "date_from": date_from, "date_to": date_to},
			"coils": {"planned": 0, "loaded": 0, "skipped": 0, "pending": 0, "progress_pct": 0},
		}

	def scoped(f, key="name"):
		if plan_names is not None:
			f[key] = ["in", plan_names]
		return f

	def n_status(st):
		return frappe.db.count("Loading Plan", scoped({"plan_status": st}))

	live = {
		"open": sum(n_status(st) for st in OPEN_STATES),
		"in_progress": n_status("In Progress"),
		"pending_approval": n_status("Pending Approval"),
		"gi_awaiting_release": frappe.db.count(
			"WMSLite SAP Confirmation", scoped({"status": "Held"}, "loading_plan")),
		"gi_failed": frappe.db.count("WMSLite SAP Confirmation", scoped(
			{"status": ["in", ("Failed (Retryable)", "Failed (Business)", "Exhausted")]},
			"loading_plan")),
	}

	rng = _date_range(date_from, date_to)
	completed_f = scoped({"plan_status": ["in", ("Completed", "Completed (Short)")]})
	loaded_f = scoped({"event_type": "Loaded"}, "loading_plan")
	reject_f = scoped({"event_type": "Rejected"}, "loading_plan")
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

	plan_filter = scoped({})
	if rng:
		plan_filter["received_at"] = rng
	rows = frappe.db.get_all("Loading Plan", filters=plan_filter,
		fields=["total_coils", "loaded_coils", "skipped_coils"], limit_page_length=0)
	planned = sum(int(r.total_coils or 0) for r in rows)
	loaded = sum(int(r.loaded_coils or 0) for r in rows)
	skipped = sum(int(r.skipped_coils or 0) for r in rows)
	return {
		"live": live,
		"throughput": throughput,
		"coils": {
			"planned": planned, "loaded": loaded, "skipped": skipped,
			"pending": planned - loaded - skipped,
			"progress_pct": round(100 * loaded / planned, 1) if planned else 0,
		},
	}


def _inventory_stats(site, material_type):
	"""Stock on hand — always "now": a live figure, never date-ranged."""
	where, vals = ["1 = 1"], {}
	if site:
		where.append(classify.site_where("bin_code", site, vals))
	if material_type:
		where.append(classify.material_where("product", material_type, vals))
	inv = frappe.db.sql(f"""SELECT COUNT(*) coils, COUNT(DISTINCT bin_code) bins,
			COALESCE(SUM(weight), 0) weight,
			SUM(CASE WHEN status = 'Picked' THEN 1 ELSE 0 END) picked
		FROM `tabBin Inventory` WHERE {" AND ".join(where)}""", vals, as_dict=True)[0]
	return {
		"coils": cint(inv.coils), "bins": cint(inv.bins),
		"weight": round(float(inv.weight or 0), 1), "picked": cint(inv.picked),
	}


def _rm_stats(date_from, date_to, site, material_type, stale_hours=4):
	"""RM Movement tiles: the floor now (Bin Inventory state) plus movements in
	range (the ledger). Site on a Bin Inventory row is the destination line's
	prefix; on the ledger it is the stored column."""
	where = ["rm_destination IS NOT NULL", "rm_destination != ''"]
	vals = {"transit": RM_IN_TRANSIT, "recvd": RM_RECEIVED}
	if site:
		where.append(classify.site_where("rm_destination", site, vals))
	if material_type:
		where.append(classify.material_where("product", material_type, vals))
	clause = " AND ".join(where)
	now = frappe.db.sql(f"""
		SELECT SUM(CASE WHEN rm_status = %(transit)s THEN 1 ELSE 0 END) in_transit,
			COUNT(DISTINCT CASE WHEN rm_status = %(transit)s THEN rm_destination END) lines_active,
			SUM(CASE WHEN rm_status = %(recvd)s AND rm_unplanned = 1 THEN 1 ELSE 0 END) flagged
		FROM `tabBin Inventory` WHERE {clause}""", vals, as_dict=True)[0]
	stale = frappe.db.sql(f"""
		SELECT COUNT(*) FROM (
			SELECT rm_destination, MIN(rm_dispatched_at) oldest
			FROM `tabBin Inventory` WHERE {clause} AND rm_status = %(transit)s
			GROUP BY rm_destination) t
		WHERE t.oldest IS NOT NULL AND t.oldest <= %(cutoff)s""",
		dict(vals, cutoff=add_to_date(now_datetime(), hours=-stale_hours)))[0][0]

	twhere = ["transaction_type IN %(types)s", "reversed = 0"]
	tvals = {"types": (RM_TXN_DISPATCH, RM_TXN_RECEIPT), "d": RM_TXN_DISPATCH, "r": RM_TXN_RECEIPT}
	if date_from:
		twhere.append("scanned_at >= %(df)s")
		tvals["df"] = f"{date_from} 00:00:00"
	if date_to:
		twhere.append("scanned_at <= %(dt)s")
		tvals["dt"] = f"{date_to} 23:59:59"
	if site:
		twhere.append("site = %(site)s")
		tvals["site"] = site
	if material_type:
		twhere.append("material_type = %(mat)s")
		tvals["mat"] = material_type
	moves = frappe.db.sql(f"""
		SELECT SUM(CASE WHEN transaction_type = %(d)s THEN 1 ELSE 0 END) dispatched,
			SUM(CASE WHEN transaction_type = %(r)s THEN 1 ELSE 0 END) received
		FROM `tabCoil Transaction` WHERE {" AND ".join(twhere)}""", tvals, as_dict=True)[0]
	return {
		"in_transit": cint(now.in_transit), "lines_active": cint(now.lines_active),
		"flagged": cint(now.flagged), "stale_lines": cint(stale), "stale_hours": stale_hours,
		"dispatched": cint(moves.dispatched), "received": cint(moves.received),
		"date_from": date_from, "date_to": date_to,
	}


@frappe.whitelist()
def get_dashboard(date_from=None, date_to=None, site=None, material_type=None, txn_type=None):
	"""Dashboard tiles, grouped by the "Transaction Type" selector.

	`txn_type` = "" / None computes every group; "loading", "inventory" or "rm"
	computes just that one, and the response only carries the keys it computed
	(`live` / `throughput` / `coils`, `inventory` / `scans`, `rm`). Live tiles are
	always "now"; range-scoped tiles say so in their own `date_from` / `date_to`.
	"""
	_require_console()
	site = site or None
	material_type = material_type or None
	groups = [txn_type] if txn_type in DASH_GROUPS else list(DASH_GROUPS)
	out = {"groups": groups,
		   "filters": {"site": site, "material_type": material_type, "txn_type": txn_type or ""}}
	if "loading" in groups:
		out.update(_loading_stats(date_from, date_to, site))
	if "inventory" in groups:
		out["inventory"] = _inventory_stats(site, material_type)
		# RM ledger rows are excluded here (they have their own tiles), so the
		# "Scans" figure reads the same under "All" and "Inventory scanning".
		out["scans"] = _scan_stats(date_from, date_to, site, material_type, exclude_rm=True)
	if "rm" in groups:
		out["rm"] = _rm_stats(date_from, date_to, site, material_type)
	return out


@frappe.whitelist()
def get_trucks(status=None, q=None, date_from=None, date_to=None, limit=100, site=None):
	_require_console()
	filters = {}
	if status:
		filters["plan_status"] = status
	if q:
		filters["truck_number"] = ["like", f"%{q}%"]
	rng = _date_range(date_from, date_to)
	if rng:
		filters["received_at"] = rng
	plan_names = _site_plan_names(site)
	if plan_names == []:
		return []
	if plan_names is not None:
		filters["name"] = ["in", plan_names]
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
def get_confirmations(status=None, date_from=None, date_to=None, limit=100, site=None):
	_require_console()
	filters = {}
	if status:
		filters["status"] = status
	rng = _date_range(date_from, date_to)
	if rng:
		filters["creation"] = rng
	plan_names = _site_plan_names(site)
	if plan_names == []:
		return []
	if plan_names is not None:
		filters["loading_plan"] = ["in", plan_names]
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
_T1_STATES = ("Pending", "Sending", "Sent", "Failed")


@frappe.whitelist()
def get_coil_loading(status=None, truck=None, date_from=None, date_to=None, limit=200, site=None):
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
	if site:
		conds.append(classify.plant_where("p.source_plant", site, params))
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


@frappe.whitelist()
def retry_count_push(txn):
	"""Re-arm a Failed in-app count scan's T1 push and enqueue the sweep."""
	_require_console()
	from sbx_wmslite import count_push

	res = count_push.requeue(txn)
	if not res.get("ok"):
		frappe.throw(_("Cannot retry: {0}").format(res.get("skipped") or "not retryable"))
	return res


@frappe.whitelist()
def retry_sap_push(txn):
	"""Re-arm a Failed Coil Transaction push, whichever push owns the row:
	an in-app Inventory Count scan (T1, count_push) or an RM line receipt (T2,
	rm_push). The console's scan feed shows both in one SAP column."""
	_require_console()
	from sbx_wmslite import count_push, rm_push

	tt = frappe.db.get_value("Coil Transaction", txn, "transaction_type")
	mod = rm_push if tt == rm_push.RM_TXN_TYPE else count_push
	res = mod.requeue(txn)
	if not res.get("ok"):
		frappe.throw(_("Cannot retry: {0}").format(res.get("skipped") or "not retryable"))
	return res


# --------------------------------------------------------------------------
# Bin Inventory — display
# --------------------------------------------------------------------------
def _bin_inventory_where(q=None, bin_code=None, status=None, site=None, material_type=None):
	where, vals = ["1 = 1"], {}
	if bin_code:
		where.append("bin_code = %(bin)s")
		vals["bin"] = bin_code
	if status:
		where.append("status = %(status)s")
		vals["status"] = status
	if q:
		where.append("(coil_barcode LIKE %(q)s OR bin_code LIKE %(q)s)")
		vals["q"] = f"{q}%"   # prefix: index-backed (see _txn_where)
	if site:
		where.append(classify.site_where("bin_code", site, vals))
	if material_type:
		where.append(classify.material_where("product", material_type, vals))
	return " AND ".join(where), vals


@frappe.whitelist()
def get_bin_inventory(q=None, bin_code=None, status=None, limit=50, start=0,
					  site=None, material_type=None):
	"""Coil-level inventory, paginated.

	`total` is the count MATCHING the filters (not the table size) — the UI shows
	"showing N of M", so a filtered search must narrow M or the pager lies.
	"""
	_require_console()
	clause, vals = _bin_inventory_where(q, bin_code, status, site, material_type)
	sel = dict(vals)
	rows = frappe.db.sql(f"""SELECT coil_barcode, bin_code, material_grade, weight, zone,
			aisle, status, source, updated_at, coil_qr_raw, sized,
			{classify.site_sql("bin_code", sel)} AS site,
			{classify.material_sql("product", sel)} AS material_type
		FROM `tabBin Inventory` WHERE {clause}
		ORDER BY updated_at DESC, coil_barcode ASC
		LIMIT %(limit)s OFFSET %(start)s""",
		dict(sel, limit=cint(limit), start=cint(start)), as_dict=True)
	# Weight (MT) as printed on the coil's QR label; coils known only by a plain
	# barcode (mostly the SAP feed) keep the stored weight the feed supplied.
	for r in rows:
		qr_w = qr_weight_mt(r.coil_qr_raw)
		if qr_w is not None:
			r.weight, r.weight_source = qr_w, "QR"
		else:
			r.weight_source = "Stored" if r.weight else ""
		# Size the same way: off the QR label (4th '-' part, decode.qr_size), else
		# the size the SAP feed stored for the coil.
		qr_s = qr_size(r.coil_qr_raw)
		if qr_s:
			r.size, r.size_source = qr_s, "QR"
		else:
			r.size = r.sized or ""
			r.size_source = "Stored" if r.size else ""
	# most-recently-touched first: with 50k+ coils the useful rows are the ones
	# that just moved, and bin_code asc pinned you to the same page.
	total = frappe.db.sql(f"SELECT COUNT(*) FROM `tabBin Inventory` WHERE {clause}", vals)[0][0]
	return {"rows": rows, "total": cint(total), "start": cint(start), "limit": cint(limit)}


@frappe.whitelist()
def get_bin_rollup(q=None, limit=50, start=0, site=None, material_type=None):
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
	if site:
		where.append(classify.site_where("bin_code", site, vals))
	if material_type:
		where.append(classify.material_where("product", material_type, vals))
	clause = " AND ".join(where)
	sel = dict(vals)
	rows = frappe.db.sql(f"""SELECT bin_code, COUNT(*) coils, COALESCE(SUM(weight), 0) weight,
			MAX(updated_at) last_updated,
			SUM(CASE WHEN status = 'Picked' THEN 1 ELSE 0 END) picked,
			MAX({classify.site_sql("bin_code", sel)}) AS site
		FROM `tabBin Inventory` WHERE {clause}
		GROUP BY bin_code ORDER BY bin_code ASC
		LIMIT %(limit)s OFFSET %(start)s""",
		dict(sel, limit=cint(limit), start=cint(start)), as_dict=True)
	total = frappe.db.sql(
		f"SELECT COUNT(DISTINCT bin_code) FROM `tabBin Inventory` WHERE {clause}", vals)[0][0]
	return {"rows": rows, "total": cint(total), "start": cint(start), "limit": cint(limit)}


@frappe.whitelist()
def get_bin_master(q=None, disabled=None, limit=50, start=0, site=None):
	"""The location master itself — every label the floor is allowed to scan.

	Distinct from `get_bin_rollup`, which reads Bin Inventory and so only knows
	about bins that currently hold stock. A supervisor chasing "why won't this bay
	scan?" needs to see the empty ones too, which is exactly the set this returns.
	"""
	_require_console()
	where, vals = ["1 = 1"], {}
	if q:
		where.append("bin_code LIKE %(q)s")   # prefix: index-backed
		vals["q"] = f"{q}%"
	if disabled not in (None, ""):
		where.append("disabled = %(disabled)s")
		vals["disabled"] = cint(disabled)
	if site:
		where.append(classify.site_where("bin_code", site, vals))
	clause = " AND ".join(where)
	sel = dict(vals)
	rows = frappe.db.sql(f"""SELECT name AS bin_code, zone, aisle, is_line, disabled, source,
			{classify.site_sql("bin_code", sel)} AS site
		FROM `tabWMSLite Bin` WHERE {clause}
		ORDER BY bin_code ASC
		LIMIT %(limit)s OFFSET %(start)s""",
		dict(sel, limit=cint(limit), start=cint(start)), as_dict=True)
	total = frappe.db.sql(f"SELECT COUNT(*) FROM `tabWMSLite Bin` WHERE {clause}", vals)[0][0]
	return {"rows": rows, "total": cint(total), "start": cint(start), "limit": cint(limit)}


# --------------------------------------------------------------------------
# scan transactions (Coil Transaction — the append-only scan feed)
# --------------------------------------------------------------------------
def _txn_where(date_from=None, date_to=None, source=None, q=None, unprocessed=0,
			   site=None, material_type=None):
	"""Shared WHERE builder for the transaction list + its counts.

	`site` / `material_type` hit the stored, indexed columns that
	CoilTransaction.before_insert fills (deriving them here cost 17 s over the
	whole feed), so even "Unassigned" is a plain equality."""
	where, vals = ["1 = 1"], {}
	if site:
		where.append("site = %(site)s")
		vals["site"] = site
	if material_type:
		where.append("material_type = %(mat)s")
		vals["mat"] = material_type
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


def _scan_stats(date_from=None, date_to=None, site=None, material_type=None, exclude_rm=False):
	"""Dashboard tiles for scan activity, scoped to the same range as throughput.

	The DISTINCT is deliberately a SEPARATE query: folded into the aggregate below
	it forces a full table scan (measured 2807ms over 260k rows), while on its own
	it rides the coil_barcode index (503ms). `exclude_rm` drops the RM ledger rows
	(dispatch / receipt / reversals) — the dashboard shows those on their own tiles.
	"""
	clause, vals = _txn_where(date_from, date_to, site=site, material_type=material_type)
	if exclude_rm:
		clause += " AND COALESCE(transaction_type, '') NOT LIKE %(rm_pat)s"
		vals["rm_pat"] = "RM %"
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
						  unprocessed=0, limit=50, start=0, site=None, material_type=None):
	"""The scan feed, newest first, paginated.

	`source` splits the two very different populations sharing this table:
	'Push API' (the SAP inventory feed) vs 'In-app' (Inventory Count scans).
	"""
	_require_console()
	clause, vals = _txn_where(date_from, date_to, source, q, unprocessed, site, material_type)
	rows = frappe.db.sql(f"""SELECT name, coil_barcode, bin_code, scanned_at, transaction_type,
			source, username, weight, material_grade, qc, processed, plant, product,
			site, material_type,
			reversed, sap_status, sap_message, sap_pushed_at
		FROM `tabCoil Transaction` WHERE {clause}
		ORDER BY scanned_at DESC, name DESC
		LIMIT %(limit)s OFFSET %(start)s""",
		dict(vals, limit=cint(limit), start=cint(start)), as_dict=True)
	total = frappe.db.sql(f"SELECT COUNT(*) FROM `tabCoil Transaction` WHERE {clause}", vals)[0][0]
	return {"rows": rows, "total": cint(total), "start": cint(start), "limit": cint(limit),
			"stats": _scan_stats(date_from, date_to, site, material_type)}


@frappe.whitelist()
def get_transaction_detail(name):
	"""One scan with its raw payload — the debugging seam for the SAP feed."""
	_require_console()
	d = frappe.db.get_value(
		"Coil Transaction", name,
		["name", "coil_barcode", "bin_code", "scanned_at", "transaction_type", "source",
		 "username", "weight", "material_grade", "qc", "plant", "product", "sized",
		 "site", "material_type",
		 "processed", "processed_at", "bin_inventory", "txn_key", "raw_json", "creation",
		 "reversed", "reversed_by", "reversed_at", "sap_status", "sap_message",
		 "sap_pushed_at", "sap_attempts", "sap_request", "sap_response"],
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
		("Bin", "bin_code"), ("Site", "site"), ("Coils", "coils"), ("Weight", "weight"),
		("Picked", "picked"), ("Last Updated", "last_updated"),
	]),
	# Column LABELS here are load-bearing: `parse_upload` suggests a mapping by
	# comparing `header.lower().replace(" ", "_")` to the dataset's column names,
	# so "Bin Code" -> bin_code and "Is Line" -> is_line. That is what lets a
	# supervisor download this sheet, add rows and upload it straight back.
	"bin_master": ("Bin Master", [
		("Bin Code", "bin_code"), ("Zone", "zone"), ("Aisle", "aisle"),
		("Is Line", "is_line"), ("Disabled", "disabled"), ("Source", "source"),
		# Derived (not a dataset column): ignored on re-upload, see _mapped_rows.
		("Site", "site"),
	]),
	"inventory_coil": ("Inventory by Coil", [
		("Bin", "bin_code"), ("Coil", "coil_barcode"), ("Site", "site"),
		("Grade", "material_grade"),
		("Weight (MT)", "weight"), ("Size", "size"), ("Zone", "zone"), ("Aisle", "aisle"),
		("Status", "status"), ("Source", "source"), ("Updated", "updated_at"),
		# The full string as scanned off the label, one cell — blank for coils
		# known only by a plain barcode.
		("Raw QR", "coil_qr_raw"),
	]),
	"transactions": ("Scan Transactions", [
		("Scanned At", "scanned_at"), ("Coil", "coil_barcode"), ("Bin", "bin_code"),
		("Site", "site"), ("Material", "material_type"),
		("Type", "transaction_type"), ("Source", "source"), ("Scanned By", "username"),
		("Weight", "weight"), ("Grade", "material_grade"), ("QC", "qc"),
		("Plant", "plant"), ("Product", "product"), ("Derived", "processed"),
		("Undone", "reversed"), ("SAP Status", "sap_status"), ("SAP Message", "sap_message"),
		("SAP Pushed At", "sap_pushed_at"),
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
		("Scanned Label", "coil_qr_raw"),
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
	# the console's global filters ride along on every export URL
	site, mat = args.get("site") or None, args.get("material_type") or None

	if view == "trucks":
		return get_trucks(status=status, q=q, date_from=df, date_to=dt, limit=limit, site=site)
	if view == "inventory_bin":
		return get_bin_rollup(q=q, limit=limit, start=0, site=site, material_type=mat)["rows"]
	if view == "bin_master":
		return get_bin_master(q=q, disabled=args.get("disabled"), limit=limit, start=0,
							  site=site)["rows"]
	if view == "inventory_coil":
		return get_bin_inventory(q=q, bin_code=args.get("bin_code"), status=status,
								 limit=limit, start=0, site=site, material_type=mat)["rows"]
	if view == "transactions":
		return get_coil_transactions(q=q, source=args.get("source"), date_from=df, date_to=dt,
									 unprocessed=args.get("unprocessed") or 0,
									 limit=limit, start=0, site=site, material_type=mat)["rows"]
	if view == "confirmations":
		return get_confirmations(status=status, date_from=df, date_to=dt, limit=limit, site=site)
	if view == "saploading":
		return get_coil_loading(status=status, truck=args.get("truck") or q,
								date_from=df, date_to=dt, limit=limit, site=site)
	if view == "saplog":
		return get_sap_log(result=args.get("result"), direction=args.get("direction"),
						   date_from=df, date_to=dt, limit=limit)
	if view == "rm":
		return get_rm_movement(status=status, destination=args.get("destination"),
							   flagged=args.get("flagged") or 0, q=q,
							   date_from=df, date_to=dt, limit=limit, start=0,
							   site=site, material_type=mat)["rows"]
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
				  "destination": "line", "flagged": "flagged only",
				  "disabled": "disabled",
				  "site": "site", "material_type": "material", "txn_type": "section"}

# Only the keys a view actually queries on: echoing an ignored param (a date on
# the inventory rollup, say) would read as a filter that never ran.
_VIEW_FILTERS = {
	"trucks": ["q", "status", "date_from", "date_to", "site"],
	"inventory_bin": ["q", "site", "material_type"],
	"bin_master": ["q", "disabled", "site"],
	"inventory_coil": ["q", "bin_code", "status", "site", "material_type"],
	"transactions": ["q", "source", "unprocessed", "date_from", "date_to", "site", "material_type"],
	"confirmations": ["status", "date_from", "date_to", "site"],
	"saploading": ["status", "truck", "q", "date_from", "date_to", "site"],
	"saplog": ["result", "direction", "date_from", "date_to"],
	"rm": ["q", "status", "destination", "flagged", "date_from", "date_to", "site", "material_type"],
	"truck_detail": ["plan"],
	"dashboard": ["date_from", "date_to", "site", "material_type", "txn_type"],
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
def export_dashboard(date_from=None, date_to=None, site=None, material_type=None, txn_type=None):
	"""The dashboard is tiles, not a table — export it as metric/value pairs,
	for exactly the sections (and filters) that are on screen."""
	_require_console()
	d = get_dashboard(date_from, date_to, site, material_type, txn_type)
	rng = "{0} → {1}".format(date_from or "beginning", date_to or "today")
	rows = []
	if "live" in d:
		rows += [
			("Loading (Outbound) — now", "Open / Active trucks", d["live"]["open"]),
			("Loading (Outbound) — now", "In Progress", d["live"]["in_progress"]),
			("Loading (Outbound) — now", "Awaiting Approval", d["live"]["pending_approval"]),
			("Loading (Outbound) — now", "GI Awaiting Release", d["live"]["gi_awaiting_release"]),
			("Loading (Outbound) — now", "GI Failed", d["live"]["gi_failed"]),
			("Loading throughput", "Completed trucks", d["throughput"]["completed"]),
			("Loading throughput", "Coils loaded", d["throughput"]["coils_loaded"]),
			("Loading throughput", "Rejects", d["throughput"]["rejects"]),
			("Coil progress", "Planned", d["coils"]["planned"]),
			("Coil progress", "Loaded", d["coils"]["loaded"]),
			("Coil progress", "Skipped", d["coils"]["skipped"]),
			("Coil progress", "Pending", d["coils"]["pending"]),
			("Coil progress", "Progress %", d["coils"]["progress_pct"]),
		]
	if "inventory" in d:
		rows += [
			("Inventory on hand", "Coils in stock", d["inventory"]["coils"]),
			("Inventory on hand", "Bins occupied", d["inventory"]["bins"]),
			("Inventory on hand", "Total weight", d["inventory"]["weight"]),
			("Inventory on hand", "Picked", d["inventory"]["picked"]),
			("Inventory scanning", "Scans", d["scans"]["total"]),
			("Inventory scanning", "From SAP feed", d["scans"]["sap_feed"]),
			("Inventory scanning", "Inventory count", d["scans"]["in_app"]),
			("Inventory scanning", "Coils touched", d["scans"]["coils"]),
			("Inventory scanning", "Unprocessed (now)", d["scans"]["unprocessed"]),
			("Inventory scanning", "Last scan", d["scans"]["last_scan"] or "—"),
		]
	if "rm" in d:
		rows += [
			("RM Movement", "In transit (now)", d["rm"]["in_transit"]),
			("RM Movement", "Lines active (now)", d["rm"]["lines_active"]),
			("RM Movement", "Ageing >= {0}h (now)".format(d["rm"]["stale_hours"]), d["rm"]["stale_lines"]),
			("RM Movement", "Flagged receipts (now)", d["rm"]["flagged"]),
			("RM Movement", "Dispatched", d["rm"]["dispatched"]),
			("RM Movement", "Received", d["rm"]["received"]),
		]
	section = dict(classify.TXN_GROUPS).get(txn_type or "", "All")
	stamp = now_datetime()
	_xlsx_response(
		"wmslite_dashboard_{0}.xlsx".format(stamp.strftime("%Y%m%d_%H%M%S")),
		"Dashboard",
		["SBX WMSLite — Dashboard",
		 "Exported {0} by {1}".format(stamp.strftime("%Y-%m-%d %H:%M"), frappe.session.user),
		 "Throughput + scan range: " + rng,
		 "Filters: " + _filter_note("dashboard", {"site": site, "material_type": material_type,
												  "txn_type": section if txn_type else ""}),
		 "Live and inventory figures are as of the export time, not the range."],
		[("Section", "section"), ("Metric", "metric"), ("Value", "value")],
		[{"section": sec, "metric": m, "value": v} for sec, m, v in rows])


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
	# The location master. Upsert-only: see `_run_binmaster_import`.
	"Bin Master": {
		"columns": ["bin_code", "zone", "aisle", "is_line", "disabled"],
		"required": ["bin_code"],
		"target": "WMSLite Bin",
		"template_rows": [["CRM6/DE1", "CRM6", "DE", "0", "0"],
						  ["PKL3/FG1", "PKL3", "FG", "0", "0"]],
		"template_headers": ["bin_code", "zone", "aisle", "is_line", "disabled"],
	},
}


def _dataset(name):
	if name not in DATASETS:
		frappe.throw(_("Unknown dataset: {0}").format(name))
	return DATASETS[name]


def _norm_header(v):
	"""Header cell -> column name, the way parse_upload matches them."""
	return str(v or "").strip().lower().replace(" ", "_")


def _skip_preamble(rows, columns):
	"""Drop title/filter lines above the real header row.

	`_xlsx_response` writes three or four caption lines and a blank row before the
	header, so a sheet exported from the console does not start with its own
	header — download-edit-reupload would otherwise map every column to a title
	string and import nothing. Scan forward to the first row that actually names a
	column of this dataset.

	No `columns`, or a row 0 that already matches, means the file is returned
	untouched, so the Loading Plan and Bin Inventory paths behave exactly as before.
	"""
	if not columns or not rows:
		return rows
	wanted = set(columns)
	for n, row in enumerate(rows):
		if wanted & {_norm_header(c) for c in row}:
			return rows[n:]
	return rows


def _read_rows(file_url, columns=None):
	from frappe.utils.xlsxutils import read_xlsx_file_from_attached_file

	fdoc = frappe.get_doc("File", {"file_url": file_url})
	if file_url.lower().endswith(".xlsx"):
		rows = read_xlsx_file_from_attached_file(fcontent=fdoc.get_content())
	else:
		import csv
		import io

		text = fdoc.get_content()
		if isinstance(text, bytes):
			text = text.decode("utf-8-sig")
		rows = list(csv.reader(io.StringIO(text)))
	return _skip_preamble(rows, columns)


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
	rows = _read_rows(file_url, spec["columns"])
	if not rows:
		frappe.throw(_("File is empty"))
	headers = [str(h).strip() for h in rows[0]]
	suggested = {}
	for col in spec["columns"]:
		for h in headers:
			if _norm_header(h) == col:
				suggested[col] = h
				break
	return {"headers": headers, "sample": rows[1:6], "columns": spec["columns"],
			"required": spec["required"], "suggested_mapping": suggested, "total_rows": len(rows) - 1}


def _mapped_rows(file_url, mapping):
	# The mapping's keys are this dataset's column names, which is all
	# `_skip_preamble` needs to find the header row in an exported sheet.
	rows = _read_rows(file_url, list(mapping))
	if not rows:
		return []
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

	if dataset == "Bin Master":
		# The one number that decides whether this upload is safe to commit is how
		# many codes are NEW, so say that outright rather than a row count. Reuses
		# the classifier the commit itself runs, so the preview cannot disagree
		# with the outcome.
		new, existing, errors = _binmaster_split(rows)
		return {"total_rows": len(rows), "plans": len(new), "group_unit": "new locations",
				"new_count": len(new), "existing_count": len(existing),
				"errors": len(errors), "sample_errors": errors[:20]}

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
		"doctype": "WMSLite Import Batch", "batch_label": label or (dataset + " upload"),
		"dataset": dataset, "target_doctype": spec["target"], "status": "Queued",
		"file_url": file_url, "file_name": file_name, "import_time": now_datetime(),
		"imported_by": frappe.session.user,
	}).insert(ignore_permissions=True)
	frappe.db.commit()
	frappe.enqueue("sbx_wmslite.console_api._run_import", queue="short", timeout=1500,
				   batch=batch.name, file_url=file_url, mapping=mapping, dataset=dataset)
	return {"batch": batch.name}


def _run_import(batch, file_url, mapping, dataset="Loading Plan"):
	b = frappe.get_doc("WMSLite Import Batch", batch)
	b.status = "Importing"
	b.save(ignore_permissions=True)
	frappe.db.commit()
	rows = _mapped_rows(file_url, mapping)

	if dataset == "Bin Master":
		inserted, errors, errlog = _run_binmaster_import(rows)
		total = len(rows)
	elif dataset == "Bin Inventory":
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


# Flag columns are only applied when the sheet actually carries a value, so a
# partial sheet (bin_code + zone, say) cannot silently clear everything else.
_BINMASTER_FLAGS = ("is_line", "disabled")
_BINMASTER_TEXT = ("zone", "aisle")


def _binmaster_split(rows):
	"""Classify rows into (new codes, existing codes, [rejection messages]).

	Shared by the preview and the commit so the count a supervisor approves is
	produced by the same code that then does the work.
	"""
	new, existing, rejects, seen = [], [], [], set()
	for n, row in enumerate(rows, start=2):
		code = (row.get("bin_code") or "").strip()
		if not code:
			rejects.append(f"Row {n}: missing bin_code")
			continue
		# A descriptive coil QR always contains " - ". Every supplied export so far
		# has had a few pasted into the location column, and one that got through
		# became a "location" holding two real coils — see locations.COIL_MARKER.
		if locations.COIL_MARKER in code:
			rejects.append(f"Row {n}: '{code[:45]}…' looks like a coil label, not a location")
			continue
		if code in seen:
			rejects.append(f"Row {n}: '{code}' appears more than once in this file")
			continue
		seen.add(code)
		(existing if frappe.db.exists("WMSLite Bin", code) else new).append((code, row))
	return new, existing, rejects


def _run_binmaster_import(rows):
	"""Upsert the location master from an uploaded sheet.

	Additive by contract: a code that is in the master but absent from the file is
	left alone, not deleted and not disabled. `WMSLite Bin.name == bin_code` and
	live Bin Inventory / Coil Transaction rows point at those names, so retiring a
	location is a deliberate act — tick `disabled` on the row, which keeps the
	history readable while taking it out of circulation.

	Codes are written verbatim. The fixture's note applies here too: the messy
	labels are what is physically printed, and normalising them on the way in
	would stop them scanning.
	"""
	new, existing, errlog = _binmaster_split(rows)
	errors = len(errlog)
	created, updated = 0, 0

	for code, row in new:
		try:
			doc = frappe.new_doc("WMSLite Bin")
			doc.bin_code = code
			for f in _BINMASTER_TEXT:
				if (row.get(f) or "").strip():
					doc.set(f, row[f].strip())
			for f in _BINMASTER_FLAGS:
				if (row.get(f) or "").strip():
					doc.set(f, cint(row[f]))
			doc.source = "Upload"
			doc.insert(ignore_permissions=True)
			created += 1
		except Exception as e:
			errors += 1
			errlog.append(f"{code}: {e}")

	for code, row in existing:
		changes = {}
		for f in _BINMASTER_TEXT:
			if (row.get(f) or "").strip():
				changes[f] = row[f].strip()
		for f in _BINMASTER_FLAGS:
			if (row.get(f) or "").strip():
				changes[f] = cint(row[f])
		if not changes:
			continue
		try:
			frappe.db.set_value("WMSLite Bin", code, changes)
			updated += 1
		except Exception as e:
			errors += 1
			errlog.append(f"{code}: {e}")

	errlog.insert(0, f"{created} new location(s) created, {updated} updated, "
					 f"{len(existing) - updated} already up to date. "
					 f"Nothing was removed.")
	return created + updated, errors, errlog


@frappe.whitelist()
def get_import_status(batch):
	_require_console()
	b = frappe.get_doc("WMSLite Import Batch", batch)
	return {"status": b.status, "total_rows": b.total_rows, "inserted": b.inserted,
			"errors": b.errors, "error_report": b.error_report,
			"done": b.status in ("Completed", "Partial", "Failed", "Rolled Back")}


@frappe.whitelist()
def delete_import_batch(batch):
	"""Roll back an upload: delete the plans it created, then the batch."""
	_require_console()
	# Only the Loading Plan path creates rows that are this batch's to delete. A
	# bin upload upserts onto a shared master that scans and stock already point
	# at, so there is no safe undo — and reporting success while doing nothing
	# would be worse than refusing.
	if frappe.db.get_value("WMSLite Import Batch", batch, "dataset") == "Bin Master":
		frappe.throw(_("A Bin Master upload can't be rolled back — it updates the shared "
					   "location master in place. To take a location out of use, tick "
					   "Disabled on it instead."))
	for name in frappe.get_all("Loading Plan", filters={"import_batch": batch}, pluck="name"):
		frappe.delete_doc("Loading Plan", name, force=1, ignore_permissions=True)
	frappe.db.set_value("WMSLite Import Batch", batch, "status", "Rolled Back")
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
def get_rm_summary(stale_hours=4, site=None, material_type=None):
	"""Per-line rollup: what is in transit, how long it has been, what is flagged.

	`oldest_hours` is the health signal. A line whose oldest in-transit coil is
	hours old is either mid-shift or has lost a coil, and the supervisor is the only
	one positioned to tell the difference.
	"""
	_require_console()
	stale_hours = cint(stale_hours) or 4
	where = ["rm_destination IS NOT NULL", "rm_destination != ''"]
	vals = {"transit": RM_IN_TRANSIT, "recvd": RM_RECEIVED}
	if site:   # a line's site is its own code prefix, like any other location
		where.append(classify.site_where("rm_destination", site, vals))
	if material_type:
		where.append(classify.material_where("product", material_type, vals))
	rows = frappe.db.sql(f"""
		SELECT rm_destination destination,
			SUM(CASE WHEN rm_status = %(transit)s THEN 1 ELSE 0 END) in_transit,
			SUM(CASE WHEN rm_status = %(transit)s THEN COALESCE(weight, 0) ELSE 0 END) transit_weight,
			SUM(CASE WHEN rm_status = %(recvd)s AND rm_unplanned = 1 THEN 1 ELSE 0 END) flagged,
			MIN(CASE WHEN rm_status = %(transit)s THEN rm_dispatched_at END) oldest_dispatch,
			MAX(CASE WHEN rm_status = %(recvd)s THEN rm_received_at END) last_receipt
		FROM `tabBin Inventory`
		WHERE {" AND ".join(where)}
		GROUP BY rm_destination
		HAVING in_transit > 0 OR flagged > 0
		ORDER BY in_transit DESC, destination ASC""",
		vals, as_dict=True)

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
					date_from=None, date_to=None, limit=200, start=0,
					site=None, material_type=None):
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
	if site:
		where.append(classify.site_where("bi.rm_destination", site, vals))
	if material_type:
		where.append(classify.material_where("bi.product", material_type, vals))
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
			bi.material_grade, bi.weight, bi.bin_code, bi.coil_qr_raw
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
