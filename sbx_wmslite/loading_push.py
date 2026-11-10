"""Outbound SAP loading transactions for SBX WMSLite.

When a coil is loaded onto a truck, record it in SAP via the zpwmsb REST
interface — one T1 transaction per loaded coil, the mirror of the inventory
scan. Uses SAP's Basic-auth + CSRF-fetch + cookie handshake (same protocol the
Jindal Google Apps Script uses), NOT the OAuth flow used by outbound_sap.py.

Flow:
  * confirm_load marks the coil sap_loading_status = "Pending" and enqueues a sweep.
  * sweep() gathers Pending loaded coils, batches them, and pushes each batch:
      GET (x-csrf-token: Fetch) -> token + cookies, then POST the T1 array.
  * The per-coil response MESSAGE ("Data Pushed Successfully") marks each coil
    Sent; a business message marks it Failed; a transport/CSRF failure leaves the
    batch Pending so the next sweep retries (SAP de-dups on BATCH+ENTRYDATE+
    ENTRYTIME, so retries are safe).

Every attempt is written to WMSLite SAP Log (direction Outbound, action Loading).
loading_push_enabled is OFF by default — nothing is sent to SAP until switched on.

The transport (`post_rows` / `row_outcome`) is the shared zpwmsb round trip: the
inventory-count push (count_push.py) sends the same T1 rows with LOCATION = bin
through it, so the CSRF handshake, response matching and test seams live here once.
"""

import base64
import json
import re

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

SUCCESS_MESSAGE = "Data Pushed Successfully"

# Sentinel PLANT when a coil has no plant anywhere (no rich QR, no plan
# source_plant, no bin) — a plain-barcode scan. Non-empty so SAP doesn't reject on
# "Plant is Mandatory", and human-readable so these scan-misses are traceable.
NO_SCAN_PLANT = "Coil Barcode not scanned"


def _settings():
	# Read fresh (not cached): this is a cold path and a stale Single could mask a
	# just-changed enable flag / endpoint / credential.
	return frappe.get_doc("WMSLite Settings")


def _basic_auth(s):
	user = s.loading_push_basic_user or ""
	pwd = s.get_password("loading_push_basic_password", raise_exception=False) or ""
	return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()


# --------------------------------------------------------------------------
# HTTP seams (isolated for mocking in tests)
# --------------------------------------------------------------------------
def _new_session():
	import requests
	return requests.Session()


def _http_get(session, url, headers, timeout, verify):
	return session.get(url, headers=headers, timeout=timeout, verify=verify)


def _http_post(session, url, data, headers, timeout, verify):
	return session.post(url, data=data, headers=headers, timeout=timeout, verify=verify)


def _csrf_token(session, s):
	"""SAP CSRF handshake on `session`: GET with x-csrf-token: Fetch -> token.

	The GET and POST share one requests.Session so the SAP session cookies are
	sent back byte-identical. (A manually rebuilt Cookie header is rejected with
	403 "CSRF token validation failed" — the SAP session-id value, which ends in
	an encoded "%3d", must match exactly; the cookie jar guarantees that.)
	"""
	timeout = cint(s.sap_gi_timeout) or 30
	verify = bool(cint(s.verify_tls))
	resp = _http_get(session, s.loading_push_url,
					 {"x-csrf-token": "Fetch", "Authorization": _basic_auth(s)},
					 timeout, verify)
	token = resp.headers.get("x-csrf-token")
	if not token:
		raise ValueError(f"CSRF fetch failed (HTTP {getattr(resp, 'status_code', '?')})")
	return token


# --------------------------------------------------------------------------
# payload
# --------------------------------------------------------------------------
def _collect_bins(codes):
	if not codes:
		return {}
	rows = frappe.get_all(
		"Bin Inventory", filters={"coil_barcode": ["in", list(set(codes))]},
		fields=["coil_barcode", "plant", "product", "sized", "weight", "material_grade"])
	return {r.coil_barcode: r for r in rows}


# --------------------------------------------------------------------------
# inventory-barcode (QR) parsing — 1:1 port of the Jindal AppSheet formula
# (parseInventoryBarcodeWithExtraCodeProd). Extracts plant/product/material/
# sized/weight from the full scanned QR so the T1 push carries them to SAP.
# --------------------------------------------------------------------------
# Prefix markers that occupy the segment *before* the batch code (OEM / grade /
# export tags etc.) — when present, the batch sits one segment later.
_EXTRA_CODE_PREFIXES = ("P1B", "P2", "RM", "API", "APISFG", "PGL", "OEM",
						"EXPORT", "GL ", "GC ", "PGI ")


def _is_extra_code(code):
	if not code:
		return False
	c = code.strip().upper()
	return any(c.startswith(p) for p in _EXTRA_CODE_PREFIXES)


def _clean_size(size_str):
	return re.sub(r"\s*Mtr\.?", "", size_str or "", flags=re.I).strip()


def _clean_weight(weight_str):
	m = re.search(r"[\d.]+", weight_str or "")
	return m.group(0) if m else ""


def parse_inventory_barcode(barcode):
	"""Parse a scanned inventory QR into T1 fields. Returns {} when the string is
	not a full inventory QR (e.g. a plain batch barcode) so callers fall back to
	their existing data instead of pushing garbage."""
	cleaned = re.sub(r"\s+", " ", barcode or "").strip()
	if not cleaned:
		return {}
	parts = [p.strip() for p in cleaned.split("-")]
	# A real inventory QR is plant-product-batch-size-weight[-material]; a plain
	# barcode splits into 1 part. Require enough segments to trust the parse.
	if len(parts) < 4:
		return {}

	plant = parts[0] or ""
	product = parts[1] if len(parts) > 1 else ""

	# Jangalpur + Galvalume is a single plant name spread across two segments.
	if plant.lower() == "jangalpur" and product.lower() == "galvalume":
		plant = f"{plant}- {product}"
		parts.pop(1)
		product = parts[1] if len(parts) > 1 else ""

	idx = 2
	if len(parts) >= 4 and _is_extra_code(parts[2]):
		idx = 3

	def g(i):
		return parts[i] if i < len(parts) else ""

	return {
		"plant": plant,
		"product": product,
		"batch": g(idx),
		"sized": _clean_size(g(idx + 1)),
		"weight": _clean_weight(g(idx + 2) or "0.01"),
		"material": g(idx + 3),
	}


def build_row(coil, plan, bin_row, s):
	"""Assemble one SAP T1 loading transaction from a loaded coil."""
	loaded = get_datetime(coil.loaded_at) if coil.get("loaded_at") else now_datetime()
	truck = (plan.truck_number if plan else "") or ""
	b = bin_row or {}
	# Extract plant/product/material/sized/weight from the scanned QR (same rules
	# as the Jindal AppSheet push). Falls back to structured coil/bin data when the
	# coil has no rich QR (e.g. a plain-barcode scan).
	p = parse_inventory_barcode(coil.get("coil_qr_raw")) if coil.get("coil_qr_raw") else {}
	return {
		"TRANSACTIONTYPE": s.loading_txn_type or "T1",
		"PLANT": p.get("plant") or (b.get("plant") if b else None) or (plan.source_plant if plan else "") or NO_SCAN_PLANT,
		"LOCATION": truck,
		"BATCH": coil.coil_barcode,
		"WEIGHT": coil.weight or p.get("weight") or (b.get("weight") if b else None) or "",
		"PRODUCT": p.get("product") or (b.get("product") if b else "") or "",
		"MATERIAL": p.get("material") or coil.material_grade or (b.get("material_grade") if b else None) or "",
		"SIZED": p.get("sized") or (b.get("sized") if b else "") or "",
		"QC": "",
		"ENTRYDATE": loaded.strftime("%d%m%Y"),
		"ENTRYTIME": loaded.strftime("%H%M%S"),
		"USERNAME": s.loading_push_user or "jindal1",
	}


def _key(row):
	return f"{row['BATCH']}_{row['ENTRYDATE']}_{row['ENTRYTIME']}"


# --------------------------------------------------------------------------
# shared zpwmsb transport (loading push + inventory-count push)
# --------------------------------------------------------------------------
def post_rows(payload, s):
	"""One SAP zpwmsb round trip: CSRF fetch + POST of `payload` (a list of T1
	rows). Never raises. Returns a dict:

	  kind         "transport_error" — exception before/at the POST (incl. CSRF);
	               "server_error"    — HTTP >= 500 (SAP-side transient);
	               "result"          — any other HTTP status, parse per row.
	  ok           True iff 2xx.
	  status_code  int, or None on transport_error.
	  text         response body ("" when none).
	  by_key       {_key(row): (MESSAGE.strip(), MSGTYPE.strip().upper())} parsed
	               from SAP's response array. MSGTYPE is "S" (success) or "E"
	               (error) when SAP supplies it, else "".
	  message      one-line summary for the SAP Log.

	The seams `_new_session` / `_csrf_token` / `_http_post` are looked up on this
	module at call time so tests can patch them for either caller.
	"""
	timeout = cint(s.sap_gi_timeout) or 30
	verify = bool(cint(s.verify_tls))
	try:
		session = _new_session()
		token = _csrf_token(session, s)
		resp = _http_post(
			session, s.loading_push_url, json.dumps(payload),
			{"Content-Type": "application/json", "Authorization": _basic_auth(s),
			 "x-csrf-token": token}, timeout, verify)
	except Exception as e:
		return {"kind": "transport_error", "ok": False, "status_code": None, "text": "",
				"by_key": {}, "message": f"Transport/CSRF: {e}"}

	by_key = {}
	try:
		parsed = json.loads(resp.text or "")
		if isinstance(parsed, list):
			for item in parsed:
				by_key[f"{item.get('BATCH')}_{item.get('ENTRYDATE')}_{item.get('ENTRYTIME')}"] = (
					(item.get("MESSAGE") or "").strip(),
					(item.get("MSGTYPE") or "").strip().upper())
	except Exception:
		pass

	ok = 200 <= resp.status_code < 300
	kind = "server_error" if resp.status_code >= 500 else "result"
	return {"kind": kind, "ok": ok, "status_code": resp.status_code,
			"text": resp.text or "", "by_key": by_key,
			"message": f"HTTP {resp.status_code}: {(resp.text or '')[:300]}"}


def row_outcome(row, res):
	"""(status, message) for one pushed row from a post_rows() "result".

	Sent iff 2xx and SAP accepted this BATCH+ENTRYDATE+ENTRYTIME: MSGTYPE "S"
	when SAP supplies it (this also covers the idempotent re-send answer "Data
	already Pushed to SAP" — a retry after a timed-out POST that SAP had in fact
	recorded), else the literal SUCCESS_MESSAGE — or 2xx with no parseable
	response list at all (every row is then taken as accepted, message "OK").
	Anything else is Failed with SAP's message, or "HTTP nnn" when SAP said
	nothing for the row.
	"""
	msg, msgtype = res["by_key"].get(_key(row), ("", ""))
	accepted = msgtype == "S" if msgtype else msg == SUCCESS_MESSAGE
	if res["ok"] and (accepted or (not res["by_key"] and msg == "")):
		return "Sent", msg or "OK"
	if not msg and res["by_key"]:
		# SAP answered for other rows but said nothing about this one (see
		# post_rows_resolving) — its fate is unknown, not rejected.
		return "Failed", f"No reply row from SAP for this coil (HTTP {res['status_code']}) — retry"
	return "Failed", msg or f"HTTP {res['status_code']}"


def post_rows_resolving(payload, s):
	"""post_rows(), then re-send one at a time every row a 2xx reply did not echo.

	Observed live 2026-09-23: a batch that holds already-pushed rows comes back
	with FEWER reply rows than were sent (a 2-row batch of duplicates answered
	with one row), so the unechoed rows' outcome is unknown — SAP may well hold
	them. A single-row re-send is safe (SAP de-dups on BATCH+ENTRYDATE+ENTRYTIME)
	and always answers ("Data already Pushed to SAP", MSGTYPE S), which settles
	each row definitively instead of marking it Failed. Replies are appended to
	`text` so the record keeps the evidence.
	"""
	res = post_rows(payload, s)
	if res["kind"] != "result" or not res["ok"] or not res["by_key"] or len(payload) < 2:
		return res
	for row in payload:
		if _key(row) in res["by_key"]:
			continue
		single = post_rows([row], s)
		if single["kind"] == "result" and single["by_key"]:
			res["by_key"].update(single["by_key"])
			res["text"] = f"{res['text']}\n{single['text']}"
	return res


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------
def _log(result, message, count=0, payload=None, action="Loading"):
	try:
		frappe.get_doc({
			"doctype": "WMSLite SAP Log", "received_at": now_datetime(),
			"direction": "Outbound", "action": action, "result": result,
			"coil_count": count, "message": (message or "")[:1000], "payload": payload,
		}).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "WMSLite loading-push log failed")


# --------------------------------------------------------------------------
# tracking doctype: WMSLite SAP Loading (one record per coil, Desk-visible)
# --------------------------------------------------------------------------
def _sync_loading_record(coil, plan, status, message=None, request_payload=None,
						 response_excerpt=None, pushed_at=None, inc_attempt=False):
	"""Upsert the WMSLite SAP Loading record mirroring a coil's T1 push state.

	`coil` is the Loading Plan Coil child-row dict (needs name/coil_barcode/weight/
	material_grade); `plan` is the plan dict (name/truck_number/sap_plan_id). Failure
	to write the tracking record must never break the push itself."""
	if not coil:
		return
	try:
		name = frappe.db.get_value("WMSLite SAP Loading", {"coil": coil.name}, "name")
		values = {
			"loading_plan": (plan.name if plan else None),
			"truck_number": (plan.truck_number if plan else None),
			"sap_plan_id": (plan.sap_plan_id if plan else None),
			"coil_barcode": coil.coil_barcode,
			"coil": coil.name,
			"status": status,
			"weight": coil.get("weight"),
			"material_grade": coil.get("material_grade"),
		}
		if message is not None:
			values["sap_message"] = message[:500]
		if request_payload is not None:
			values["request_payload"] = request_payload
		if response_excerpt is not None:
			values["response_excerpt"] = response_excerpt
		if pushed_at is not None:
			values["last_pushed_at"] = pushed_at
		if name:
			if inc_attempt:
				values["attempts"] = cint(frappe.db.get_value("WMSLite SAP Loading", name, "attempts")) + 1
			frappe.db.set_value("WMSLite SAP Loading", name, values)
		else:
			values["doctype"] = "WMSLite SAP Loading"
			values["attempts"] = 1 if inc_attempt else 0
			frappe.get_doc(values).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "WMSLite SAP Loading sync failed")


def _set_record_status(coil_name, status):
	"""Flip an existing record's status by coil (no-op if none exists)."""
	name = frappe.db.get_value("WMSLite SAP Loading", {"coil": coil_name}, "name")
	if name:
		frappe.db.set_value("WMSLite SAP Loading", name, "status", status)


# --------------------------------------------------------------------------
# push
# --------------------------------------------------------------------------
def push_batch(chunk, plans, bins, s):
	"""Push one batch of loaded coils. chunk = list of child-row dicts (with
	`name`, `parent`, coil fields). Updates each coil's sap_loading_status."""
	payload, key_to_coil, coil_meta = [], {}, {r.name: r for r in chunk}
	for r in chunk:
		row = build_row(r, plans.get(r.parent), bins.get(r.coil_barcode), s)
		payload.append(row)
		key_to_coil[_key(row)] = r.name

	res = post_rows_resolving(payload, s)
	if res["kind"] in ("transport_error", "server_error"):
		# Transport / CSRF failure or SAP-side 5xx: hand the claimed batch back
		# (Sending -> Pending) for the next sweep.
		_release([r.name for r in chunk])
		_log("Error", res["message"], count=len(payload), payload=json.dumps(payload))
		frappe.db.commit()
		return {"sent": 0, "failed": 0, "deferred": len(payload)}

	now, sent, failed = now_datetime(), 0, 0
	for row in payload:
		coil_name = key_to_coil[_key(row)]
		status, msg = row_outcome(row, res)
		if status == "Sent":
			sent += 1
		else:
			failed += 1
		frappe.db.set_value("Loading Plan Coil", coil_name, {
			"sap_loading_status": status, "sap_loading_message": msg[:500],
			"sap_loading_pushed_at": now}, update_modified=False)
		_sync_loading_record(coil_meta.get(coil_name), plans.get(coil_meta[coil_name].parent),
							  status, message=msg, request_payload=json.dumps(row, indent=2),
							  response_excerpt=res["text"][:4000], pushed_at=now, inc_attempt=True)

	_log("Accepted" if res["ok"] else "Rejected",
		 f"pushed {len(payload)} (sent {sent}, failed {failed}), HTTP {res['status_code']}",
		 count=len(payload), payload=json.dumps(payload))
	frappe.db.commit()
	return {"sent": sent, "failed": failed, "deferred": 0}


# A coil claimed ("Sending") this long ago with no answer was orphaned by a dead
# worker: put it back to Pending. >= 2 cron ticks; also >= 2*timeout+60s.
STUCK_SECONDS = 600

_ROW_FIELDS = ["name", "parent", "coil_barcode", "weight", "material_grade", "loaded_at",
			   "coil_qr_raw"]


def _recover_stuck(s):
	"""Coils a dead worker left in "Sending" go back to Pending."""
	horizon = max(STUCK_SECONDS, 2 * (cint(s.sap_gi_timeout) or 30) + 60)
	cutoff = add_to_date(now_datetime(), seconds=-horizon)
	frappe.db.sql("""
		UPDATE `tabLoading Plan Coil`
		SET sap_loading_status = 'Pending'
		WHERE sap_loading_status = 'Sending'
		  AND (sap_loading_pushed_at IS NULL OR sap_loading_pushed_at < %(cutoff)s)""",
		{"cutoff": cutoff})


def _claim(limit):
	"""Atomically move up to `limit` Pending loaded coils to "Sending"; return them.

	Seen live 2026-09-23: a synchronous sweep and the worker's enqueued sweep ran
	0.27 s apart and POSTed the same two coils twice (SAP de-duped, so no harm
	done, but only by luck of SAP's key). The locking SELECT serialises
	overlapping sweeps: the second blocks on the row locks, then its conditional
	UPDATE matches nothing because the rows are already "Sending".
	"""
	names = [r[0] for r in frappe.db.sql("""
		SELECT name FROM `tabLoading Plan Coil`
		WHERE sap_loading_status = 'Pending' AND coil_status = 'Loaded'
		ORDER BY loaded_at ASC, creation ASC
		LIMIT %(limit)s
		FOR UPDATE""", {"limit": cint(limit)})]
	if not names:
		frappe.db.commit()
		return []
	frappe.db.sql("""
		UPDATE `tabLoading Plan Coil`
		SET sap_loading_status = 'Sending', sap_loading_pushed_at = %(now)s
		WHERE name IN %(names)s AND sap_loading_status = 'Pending'""",
		{"now": now_datetime(), "names": tuple(names)})
	frappe.db.commit()
	return frappe.get_all("Loading Plan Coil",
						  filters={"name": ["in", names], "sap_loading_status": "Sending"},
						  fields=_ROW_FIELDS, order_by="loaded_at asc, creation asc")


def _release(names):
	"""Transport / 5xx: hand the claimed coils back to the next sweep."""
	if names:
		frappe.db.sql("""UPDATE `tabLoading Plan Coil` SET sap_loading_status = 'Pending'
			WHERE name IN %(names)s AND sap_loading_status = 'Sending'""", {"names": tuple(names)})


def sweep(limit=2000):
	"""Push all Pending loaded coils to SAP in batches. Scheduled + enqueued."""
	s = _settings()
	if not cint(s.loading_push_enabled):
		return {"skipped": "disabled"}
	_recover_stuck(s)
	rows = _claim(limit)
	if not rows:
		return {"pending": 0}

	plans = {p.name: p for p in frappe.get_all(
		"Loading Plan", filters={"name": ["in", list({r.parent for r in rows})]},
		fields=["name", "truck_number", "sap_plan_id", "source_plant"])}
	bins = _collect_bins([r.coil_barcode for r in rows])

	batch = cint(s.loading_push_batch_size) or 200
	totals = {"sent": 0, "failed": 0, "deferred": 0, "pending": len(rows)}
	for i in range(0, len(rows), batch):
		res = push_batch(rows[i:i + batch], plans, bins, s)
		for k in ("sent", "failed", "deferred"):
			totals[k] += res.get(k, 0)
	return totals


# --------------------------------------------------------------------------
# triggers (called from api.confirm_load / undo)
# --------------------------------------------------------------------------
def on_coil_loaded(plan_doc, coil_row):
	"""Mark a freshly-loaded coil Pending and enqueue a push (no-op if disabled).
	Also opens its Desk-visible tracking record so 'not yet pushed' coils show.

	When auto-completion is off the coil is parked at 'Deferred' instead — the
	sweep ignores Deferred, so no T1 fires until the operator completes the plan
	(arm_deferred flips them to Pending). This keeps the T1 push and the PGI
	goods-issue on the same manual trigger."""
	s = _settings()
	if not cint(s.loading_push_enabled):
		# Push feature off — still record the coil so it's Desk-visible in the
		# WMSLite SAP Loading list (not silently lost). It parks at 'Disabled';
		# nothing is sent. Enable the setting later, then push_disabled / Push Now.
		frappe.db.set_value("Loading Plan Coil", coil_row.name, "sap_loading_status",
							"Disabled", update_modified=False)
		_sync_loading_record(coil_row, plan_doc, "Disabled")
		return
	deferred = not _auto_complete_enabled(s)
	status = "Deferred" if deferred else "Pending"
	frappe.db.set_value("Loading Plan Coil", coil_row.name, "sap_loading_status",
						status, update_modified=False)
	_sync_loading_record(coil_row, plan_doc, status)
	if not deferred:
		frappe.enqueue("sbx_wmslite.loading_push.sweep", queue="short",
					   enqueue_after_commit=True)


def _auto_complete_enabled(s):
	"""Auto-complete switch off the settings doc; defaults OFF when unset (a
	freshly-migrated Single field reads as None until first saved). OFF means each
	loaded coil's T1 push is parked at 'Deferred' until the plan is completed."""
	val = s.get("auto_complete_on_full_load")
	return False if val is None else bool(cint(val))


def arm_deferred(plan_name):
	"""Release a completed plan's Deferred coils to Pending and enqueue the sweep.
	Called from the manual-complete / short-load-approval paths. No-op if disabled."""
	if not cint(_settings().loading_push_enabled):
		return {"skipped": "disabled"}
	rows = frappe.get_all(
		"Loading Plan Coil",
		filters={"parent": plan_name, "coil_status": "Loaded",
				 "sap_loading_status": "Deferred"},
		pluck="name")
	for name in rows:
		frappe.db.set_value("Loading Plan Coil", name, "sap_loading_status",
							"Pending", update_modified=False)
		_set_record_status(name, "Pending")
	if rows:
		frappe.enqueue("sbx_wmslite.loading_push.sweep", queue="short",
					   enqueue_after_commit=True)
	return {"armed": len(rows)}


def on_coil_unloaded(coil_row):
	"""A coil was undone. Cancel a not-yet-sent push; leave an already-sent one
	(the SAP record stands — reconcile manually). Deferred (never armed) is
	treated like Pending — the push had not gone out yet."""
	if frappe.db.get_value("Loading Plan Coil", coil_row.name, "sap_loading_status") in ("Pending", "Deferred", "Disabled"):
		frappe.db.set_value("Loading Plan Coil", coil_row.name, "sap_loading_status",
							"Not Required", update_modified=False)
		_set_record_status(coil_row.name, "Not Required")


def requeue(coil_name):
	"""Re-arm a Failed coil's T1 push (console retry). Only Failed rows are
	re-armed — re-sending a Sent coil would just create a confusing duplicate log
	line (SAP itself de-dups on BATCH+ENTRYDATE+ENTRYTIME). No-op if disabled."""
	if not cint(_settings().loading_push_enabled):
		return {"skipped": "disabled"}
	status, coil_status = frappe.db.get_value(
		"Loading Plan Coil", coil_name, ["sap_loading_status", "coil_status"])
	if coil_status != "Loaded":
		return {"skipped": "not loaded"}
	if status != "Failed":
		return {"skipped": status or "unknown"}
	frappe.db.set_value("Loading Plan Coil", coil_name, "sap_loading_status",
						"Pending", update_modified=False)
	_set_record_status(coil_name, "Pending")
	frappe.enqueue("sbx_wmslite.loading_push.sweep", queue="short",
				   enqueue_after_commit=True)
	return {"ok": True}


@frappe.whitelist()
def trigger_push(loading):
	"""Push Now / Reset & Retry from the WMSLite SAP Loading form. Arms the coil
	and enqueues the sweep. Already-Sent coils are refused (SAP already has them)."""
	frappe.only_for(("Loading Supervisor", "System Manager"))
	if not cint(_settings().loading_push_enabled):
		return {"skipped": "loading push disabled in WMSLite Settings"}
	coil_name = frappe.db.get_value("WMSLite SAP Loading", loading, "coil")
	if not coil_name:
		return {"skipped": "no linked coil"}
	status, coil_status = frappe.db.get_value(
		"Loading Plan Coil", coil_name, ["sap_loading_status", "coil_status"])
	if coil_status != "Loaded":
		return {"skipped": "coil is not Loaded"}
	if status == "Sent":
		return {"skipped": "already sent to SAP"}
	frappe.db.set_value("Loading Plan Coil", coil_name, "sap_loading_status",
						"Pending", update_modified=False)
	frappe.db.set_value("WMSLite SAP Loading", loading, "status", "Pending")
	frappe.enqueue("sbx_wmslite.loading_push.sweep", queue="short",
				   enqueue_after_commit=True)
	return {"ok": True}


@frappe.whitelist()
def push_disabled(plan=None):
	"""Arm coils parked at 'Disabled' (loaded while push was off) → Pending, then
	sweep. Optionally scoped to one Loading Plan. Use after turning the setting on
	to send the backlog. No-op while the feature is still disabled."""
	frappe.only_for(("Loading Supervisor", "System Manager"))
	if not cint(_settings().loading_push_enabled):
		return {"skipped": "loading push disabled in WMSLite Settings"}
	filters = {"sap_loading_status": "Disabled", "coil_status": "Loaded"}
	if plan:
		filters["parent"] = plan
	rows = frappe.get_all("Loading Plan Coil", filters=filters, pluck="name")
	for name in rows:
		frappe.db.set_value("Loading Plan Coil", name, "sap_loading_status",
							"Pending", update_modified=False)
		_set_record_status(name, "Pending")
	if rows:
		frappe.enqueue("sbx_wmslite.loading_push.sweep", queue="short",
					   enqueue_after_commit=True)
	return {"armed": len(rows)}


def backfill_coil_qr():
	"""One-off: copy the scanned QR from each coil's Loaded event onto the coil's
	`coil_qr_raw`, so already-loaded coils (incl. Failed pushes) can be re-pushed
	with plant/product/material parsed from the QR. Only fills empty ones, and only
	when the event's scanned_qr is richer than the bare barcode."""
	coils = frappe.get_all(
		"Loading Plan Coil",
		filters={"coil_status": "Loaded", "coil_qr_raw": ["in", (None, "")]},
		fields=["name", "parent", "coil_barcode"])
	filled = 0
	for c in coils:
		ev = frappe.get_all(
			"Coil Load Event",
			filters={"loading_plan": c.parent, "coil_barcode": c.coil_barcode,
					 "event_type": "Loaded"},
			fields=["scanned_qr"], order_by="creation desc", limit_page_length=1)
		qr = (ev[0].scanned_qr if ev else "") or ""
		if qr and qr != c.coil_barcode:
			frappe.db.set_value("Loading Plan Coil", c.name, "coil_qr_raw", qr,
								update_modified=False)
			filled += 1
	frappe.db.commit()
	return {"scanned": len(coils), "filled": filled}


def backfill_records():
	"""One-off: create tracking records for coils already pushed (or queued) that
	predate this doctype. Idempotent — skips coils that already have a record."""
	rows = frappe.get_all(
		"Loading Plan Coil",
		filters={"sap_loading_status": ["in", ("Disabled", "Pending", "Sent", "Failed", "Not Required")]},
		fields=["name", "parent", "coil_barcode", "weight", "material_grade",
				"sap_loading_status", "sap_loading_message", "sap_loading_pushed_at"])
	if not rows:
		return {"created": 0}
	plans = {p.name: p for p in frappe.get_all(
		"Loading Plan", filters={"name": ["in", list({r.parent for r in rows})]},
		fields=["name", "truck_number", "sap_plan_id"])}
	existing = set(frappe.get_all("WMSLite SAP Loading", pluck="coil"))
	created = 0
	for r in rows:
		if r.name in existing:
			continue
		_sync_loading_record(r, plans.get(r.parent), r.sap_loading_status,
							  message=r.sap_loading_message, pushed_at=r.sap_loading_pushed_at)
		created += 1
	frappe.db.commit()
	return {"created": created}
