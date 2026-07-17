"""Back-office console endpoints for SBX WMSLite (Loading Supervisor).

Dashboard KPIs, truck drill-down, the inbound SAP log, and a guided upload
fallback for loading plans (parse -> preview -> commit background job ->
rollback). All endpoints require the Loading Supervisor role.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, getdate, now_datetime, today

SUPERVISOR = "Loading Supervisor"
OPEN_STATES = ("Open", "In Progress", "Pending Approval")


def _require_console():
	if SUPERVISOR not in frappe.get_roles() and "System Manager" not in frappe.get_roles():
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

	return {
		"live": live,
		"throughput": throughput,
		"coils": {
			"planned": planned, "loaded": loaded, "skipped": skipped,
			"pending": planned - loaded - skipped,
			"progress_pct": round(100 * loaded / planned, 1) if planned else 0,
		},
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
def get_bin_inventory(q=None, bin_code=None, status=None, limit=200):
	_require_console()
	filters = {}
	if bin_code:
		filters["bin_code"] = bin_code
	if status:
		filters["status"] = status
	or_filters = None
	if q:
		or_filters = [["coil_barcode", "like", f"%{q}%"], ["bin_code", "like", f"%{q}%"]]
	rows = frappe.get_all(
		"Bin Inventory", filters=filters, or_filters=or_filters,
		fields=["name as coil_barcode", "bin_code", "material_grade", "weight", "zone",
				"aisle", "status", "source", "updated_at"],
		order_by="bin_code asc, coil_barcode asc", limit_page_length=cint(limit),
	)
	total = frappe.db.count("Bin Inventory")
	bins = len({r.bin_code for r in rows})
	return {"rows": rows, "total": total, "bins_shown": bins}


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
