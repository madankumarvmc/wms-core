"""Outbound SAP goods-issue confirmation for SBX WMSLite.

When a truck finishes loading, post a goods-issue (GI) confirmation to SAP.

Design (locked):
  * One GI per truck at completion; loaded coils are issued, skipped coils are
    informational only.
  * Auto-send on full `Completed`; `Completed (Short)` is HELD for a supervisor
    to release from the console.
  * REST/JSON over HTTPS, OAuth2 client-credentials (token cached in Redis).
  * Fully async (RQ jobs) — the operator/console action never blocks on SAP.
  * Idempotent: one WMSLite SAP Confirmation per plan (unique key) AND an
    Idempotency-Key header, so a retry after a lost response never double-posts.
  * Retries with exponential backoff on transport/5xx/429; 4xx business errors
    are parked for a supervisor; a scheduler sweep drives retries.

Every attempt is written to WMSLite SAP Log (direction=Outbound), secrets redacted.
"""

import json

import frappe
from frappe.utils import add_to_date, cint, now_datetime

TOKEN_CACHE_KEY = "wmslite_sap_oauth_token"
SENDABLE = ("Pending", "Failed (Retryable)")
BACKOFF_BASE_S = 60
BACKOFF_CAP_S = 3600


# --------------------------------------------------------------------------
# settings / token
# --------------------------------------------------------------------------
def _settings():
	# Read fresh (not cached): completion + scheduler are cold paths, and a stale
	# cached Single can mask a just-changed gi_enabled / endpoint / credential.
	return frappe.get_doc("WMSLite Settings")


def _get_token(force_refresh=False):
	"""OAuth2 client-credentials token, cached in Redis until ~60s before expiry."""
	if not force_refresh:
		cached = frappe.cache().get_value(TOKEN_CACHE_KEY)
		if cached:
			return cached

	import requests

	s = _settings()
	data = {"grant_type": "client_credentials",
			"client_id": s.sap_client_id,
			"client_secret": s.get_password("sap_client_secret", raise_exception=False)}
	if s.sap_scope:
		data["scope"] = s.sap_scope
	resp = requests.post(s.sap_token_url, data=data, timeout=cint(s.sap_gi_timeout) or 30,
						 verify=bool(cint(s.verify_tls)))
	resp.raise_for_status()
	body = resp.json()
	token = body.get("access_token")
	if not token:
		raise ValueError("Token endpoint returned no access_token")
	ttl = max(60, cint(body.get("expires_in") or 3600) - 60)
	frappe.cache().set_value(TOKEN_CACHE_KEY, token, expires_in_sec=ttl)
	return token


def _http_post(url, payload, token, timeout, verify, idempotency_key):
	"""POST the GI payload. Isolated for easy mocking in tests."""
	import requests

	headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}",
			   "Idempotency-Key": idempotency_key}
	return requests.post(url, data=json.dumps(payload), headers=headers,
						 timeout=timeout, verify=verify)


# --------------------------------------------------------------------------
# payload
# --------------------------------------------------------------------------
def build_payload(plan):
	"""Assemble the GI confirmation body from a Loading Plan doc."""
	loaded, skipped = [], []
	for c in plan.coils:
		if c.coil_status == "Loaded":
			loaded.append({"coil_barcode": c.coil_barcode, "material_grade": c.material_grade,
						   "weight": c.weight, "loaded_at": str(c.loaded_at) if c.loaded_at else None,
						   "unplanned": cint(c.unplanned)})
		elif c.coil_status == "Skipped":
			skipped.append({"coil_barcode": c.coil_barcode, "reason": c.skip_reason})
	return {
		"idempotency_key": plan.name,
		"sap_plan_id": plan.sap_plan_id,
		"truck_number": plan.truck_number,
		"completion_type": "Short" if plan.plan_status == "Completed (Short)" else "Full",
		"completed_at": str(plan.gi_confirmed_at or now_datetime()),
		"approved_by": plan.approved_by,
		"loaded_coils": loaded,
		"skipped_coils": skipped,  # informational — not issued
	}


# --------------------------------------------------------------------------
# logging / plan sync
# --------------------------------------------------------------------------
def _log(conf, result, message, http_status=None):
	try:
		frappe.get_doc({
			"doctype": "WMSLite SAP Log", "received_at": now_datetime(),
			"direction": "Outbound", "action": "Goods Issue", "result": result,
			"truck_number": conf.truck_number, "sap_plan_id": conf.sap_plan_id,
			"loading_plan": conf.loading_plan, "coil_count": 0,
			"message": (message or "")[:1000],
			# payload (no secrets); never log token/credentials
			"payload": conf.payload,
		}).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "WMSLite outbound log failed")


_PLAN_GI = {"Held": "Held", "Pending": "Pending", "Sending": "Pending",
			"Confirmed": "Confirmed", "Failed (Retryable)": "Failed",
			"Failed (Business)": "Failed", "Exhausted": "Failed", "Cancelled": "Not Required"}


def _sync_plan(conf):
	frappe.db.set_value("Loading Plan", conf.loading_plan, {
		"gi_status": _PLAN_GI.get(conf.status, "Pending"),
		"gi_confirmation": conf.name,
		"gi_sap_document": conf.sap_gi_document,
		"gi_confirmed_at": conf.confirmed_at,
	}, update_modified=False)


# --------------------------------------------------------------------------
# lifecycle: create on completion
# --------------------------------------------------------------------------
def on_plan_completed(plan):
	"""Called when a Loading Plan transitions into a completed state."""
	s = _settings()
	if not cint(s.gi_enabled):
		return
	key = plan.name
	if frappe.db.exists("WMSLite SAP Confirmation", {"idempotency_key": key}):
		return  # already created (idempotent) — never a second GI

	is_short = plan.plan_status == "Completed (Short)"
	hold = is_short and cint(s.gi_hold_short_for_review)
	auto_full = (not is_short) and cint(s.gi_auto_send_full)

	conf = frappe.get_doc({
		"doctype": "WMSLite SAP Confirmation",
		"loading_plan": plan.name, "truck_number": plan.truck_number,
		"sap_plan_id": plan.sap_plan_id,
		"confirmation_type": "Short" if is_short else "Full",
		"idempotency_key": key,
		"status": "Held" if hold else "Pending",
		"payload": json.dumps(build_payload(plan), indent=2),
		"attempts": 0,
	})
	conf.insert(ignore_permissions=True)
	_sync_plan(conf)
	frappe.db.commit()

	if conf.status == "Pending" and (auto_full or not is_short):
		frappe.enqueue("sbx_wmslite.outbound_sap.send", queue="short",
					   confirmation=conf.name, enqueue_after_commit=True)


# --------------------------------------------------------------------------
# send (worker)
# --------------------------------------------------------------------------
def _schedule_retry(conf, max_attempts):
	if conf.attempts >= max_attempts:
		conf.status = "Exhausted"
		conf.next_attempt_at = None
	else:
		delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** (conf.attempts - 1)))
		conf.status = "Failed (Retryable)"
		conf.next_attempt_at = add_to_date(now_datetime(), seconds=delay)


def send(confirmation):
	"""Background worker: post one confirmation to SAP. Idempotent + safe to retry."""
	# atomic claim: only proceed from a sendable state, flip to Sending
	current = frappe.db.get_value("WMSLite SAP Confirmation", confirmation, "status", for_update=True)
	if current not in SENDABLE:
		frappe.db.commit()
		return {"skipped": current}
	frappe.db.set_value("WMSLite SAP Confirmation", confirmation, "status", "Sending")
	frappe.db.commit()

	conf = frappe.get_doc("WMSLite SAP Confirmation", confirmation)
	s = _settings()
	if not cint(s.gi_enabled):
		conf.status = "Pending"
		conf.save(ignore_permissions=True)
		frappe.db.commit()
		return {"skipped": "gi_disabled"}

	payload = json.loads(conf.payload)
	timeout = cint(s.sap_gi_timeout) or 30
	verify = bool(cint(s.verify_tls))
	max_attempts = cint(s.gi_max_attempts) or 8
	conf.attempts = cint(conf.attempts) + 1
	conf.last_attempt_at = now_datetime()

	try:
		token = _get_token()
		resp = _http_post(s.sap_gi_url, payload, token, timeout, verify, conf.idempotency_key)
		if resp.status_code == 401:  # token stale → refresh once and replay
			token = _get_token(force_refresh=True)
			resp = _http_post(s.sap_gi_url, payload, token, timeout, verify, conf.idempotency_key)

		conf.http_status = str(resp.status_code)
		conf.response_excerpt = (resp.text or "")[:500]

		if 200 <= resp.status_code < 300:
			conf.status = "Confirmed"
			conf.confirmed_at = now_datetime()
			conf.sap_gi_document = _extract_gi_doc(resp)
			conf.last_error = None
			result, msg = "Accepted", f"GI confirmed (doc {conf.sap_gi_document or 'n/a'})"
		elif resp.status_code in (408, 429) or resp.status_code >= 500:
			conf.last_error = f"Transport/{resp.status_code}: {conf.response_excerpt}"
			_schedule_retry(conf, max_attempts)
			result, msg = "Error", conf.last_error
		else:  # other 4xx = business error → park for supervisor
			conf.status = "Failed (Business)"
			conf.last_error = f"Business/{resp.status_code}: {conf.response_excerpt}"
			result, msg = "Rejected", conf.last_error
	except Exception as e:
		conf.http_status = None
		conf.last_error = f"{type(e).__name__}: {e}"
		_schedule_retry(conf, max_attempts)
		result, msg = "Error", conf.last_error

	conf.save(ignore_permissions=True)
	_sync_plan(conf)
	_log(conf, result, msg, conf.http_status)
	frappe.db.commit()
	return {"status": conf.status, "gi_document": conf.sap_gi_document}


def _extract_gi_doc(resp):
	try:
		b = resp.json()
		for k in ("gi_document", "material_document", "materialDocument", "documentNumber", "document_no"):
			if b.get(k):
				return str(b[k])
	except Exception:
		pass
	return None


# --------------------------------------------------------------------------
# supervisor actions + scheduler sweep
# --------------------------------------------------------------------------
def release(confirmation, user=None):
	"""Release a Held short-load confirmation for sending."""
	conf = frappe.get_doc("WMSLite SAP Confirmation", confirmation)
	if conf.status != "Held":
		frappe.throw(f"Confirmation is {conf.status}, not Held")
	conf.status = "Pending"
	conf.released_by = user or frappe.session.user
	conf.next_attempt_at = None
	conf.save(ignore_permissions=True)
	_sync_plan(conf)
	frappe.db.commit()
	frappe.enqueue("sbx_wmslite.outbound_sap.send", queue="short",
				   confirmation=conf.name, enqueue_after_commit=True)
	return {"ok": True}


def manual_retry(confirmation):
	"""Reset a failed/exhausted confirmation and send now."""
	conf = frappe.get_doc("WMSLite SAP Confirmation", confirmation)
	if conf.status in ("Confirmed", "Sending"):
		frappe.throw(f"Confirmation is {conf.status}")
	conf.status = "Pending"
	conf.next_attempt_at = None
	conf.save(ignore_permissions=True)
	_sync_plan(conf)
	frappe.db.commit()
	frappe.enqueue("sbx_wmslite.outbound_sap.send", queue="short",
				   confirmation=conf.name, enqueue_after_commit=True)
	return {"ok": True}


def retry_sweep():
	"""Scheduler: drive due retries and recover stuck 'Sending' rows."""
	s = _settings()
	if not cint(s.gi_enabled):
		return
	now = now_datetime()
	# due Pending / retryable
	due = frappe.get_all("WMSLite SAP Confirmation",
		filters={"status": ["in", SENDABLE]},
		fields=["name", "next_attempt_at"])
	for r in due:
		if not r.next_attempt_at or r.next_attempt_at <= now:
			frappe.enqueue("sbx_wmslite.outbound_sap.send", queue="short", confirmation=r.name)

	# recover rows stuck in Sending (worker died) older than 2x timeout
	stuck_before = add_to_date(now, seconds=-2 * (cint(s.sap_gi_timeout) or 30) - 60)
	for name in frappe.get_all("WMSLite SAP Confirmation",
			filters={"status": "Sending", "last_attempt_at": ["<", stuck_before]}, pluck="name"):
		frappe.db.set_value("WMSLite SAP Confirmation", name, "status", "Failed (Retryable)")
	frappe.db.commit()
