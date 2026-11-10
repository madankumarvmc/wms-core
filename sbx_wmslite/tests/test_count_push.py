"""Unit tests for the outbound SAP push of in-app Inventory Count scans (mocked HTTP).

The CSRF/HTTP seams on loading_push are mocked (count_push.post_rows resolves them
there), so these tests NEVER call a real SAP endpoint. All data is __TEST__-
prefixed and torn down; every WMSLite Settings field touched is restored.

    bench --site <site> run-tests --app sbx_wmslite --module sbx_wmslite.tests.test_count_push
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from sbx_wmslite import api, count_api
from sbx_wmslite import count_push as cp
from sbx_wmslite import loading_push as lp
from sbx_wmslite.bin_api import upsert_bin
from sbx_wmslite.tests.settings_guard import SettingsGuard, assert_no_live_pending_pushes

BIN_A = "__TEST__CPBIN_A"
BIN_B = "__TEST__CPBIN_B"
SUCCESS = "Data Pushed Successfully"
FULL_QR = "Jangalpur Al Foil - ALUMINIUM FOIL BARE-J2603B111 - 6.000 mm  X 1280 - 720.500 KG- UL06000"


class FakeResp:
	def __init__(self, status_code, text):
		self.status_code = status_code
		self.text = text


# Every field these tests write is listed here so tearDownClass can put the
# site's own values back — this Single is shared with the live site.
SETTINGS = SettingsGuard(
	"count_enabled",
	"count_push_enabled",
	"count_txn_type",
	"loading_push_url",
	"loading_push_user",
	"loading_push_batch_size",
	"verify_tls",
	"qr_decode_rule",
	"bin_decode_rule",
	"bin_validation_enabled",
)


def _settings(push=1, txn_type="T1"):
	SETTINGS.apply(
		count_enabled=1,
		count_push_enabled=push,
		count_txn_type=txn_type,
		loading_push_url="https://sap.example.invalid/sap/bc/zpwmsb/rest",
		loading_push_user="jindal1",
		loading_push_batch_size=200,
		verify_tls=0,
		# Identity decode: the scanned string IS the coil id. __TEST__ bins are not
		# in the location master, so validation is off (covered in test_locations).
		qr_decode_rule="Identity", bin_decode_rule="Identity", bin_validation_enabled=0,
	)


def _cleanup():
	for dt, field in (("Coil Transaction", "coil_barcode"), ("Bin Inventory", "coil_barcode")):
		for n in frappe.get_all(dt, filters={field: ["like", "__TEST__%"]}, pluck="name"):
			frappe.delete_doc(dt, n, force=1, ignore_permissions=True)
	for n in frappe.get_all("WMSLite SAP Log", filters={"action": "Inventory Count",
														 "payload": ["like", "%__TEST__%"]}, pluck="name"):
		frappe.delete_doc("WMSLite SAP Log", n, force=1, ignore_permissions=True)
	frappe.db.commit()


def _echo_success(session, url, data, headers, timeout, verify):
	rows = json.loads(data)
	out = [{"BATCH": r["BATCH"], "ENTRYDATE": r["ENTRYDATE"], "ENTRYTIME": r["ENTRYTIME"],
			"MESSAGE": SUCCESS} for r in rows]
	return FakeResp(200, json.dumps(out))


class TestCountPush(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_cleanup()

	@classmethod
	def tearDownClass(cls):
		_cleanup()
		SETTINGS.restore()
		super().tearDownClass()

	def setUp(self):
		# sweep() is site-wide and SAP is mocked below: a real Pending scan would be
		# marked Sent by the echo. Refuse rather than corrupt (see settings_guard).
		assert_no_live_pending_pushes()
		frappe.set_user("Administrator")
		_cleanup()
		_settings(1)

	# ---- helpers ----
	def _coil(self, n=1):
		return "__TEST__CP_" + self._testMethodName + "_" + str(n)

	def _scan(self, coil, bin_code=BIN_A, cid=None, qc=0):
		"""count_scan with the sweep enqueue swallowed; returns the txn name."""
		with patch.object(frappe, "enqueue"):
			count_api.count_scan(bin_code, coil, client_event_id=cid, qc=qc)
		return frappe.db.get_value("Coil Transaction", {"coil_barcode": coil, "reversed": 0,
														 "transaction_type": "Inventory Count"}, "name")

	def _mktxn(self, coil, source="Push API", transaction_type=None, sap_status=None,
			   reversed=0, bin_code=BIN_A, scanned_at=None):
		"""A raw Coil Transaction of any population (feed / upload / RM / count)."""
		d = frappe.get_doc({
			"doctype": "Coil Transaction", "coil_barcode": coil, "bin_code": bin_code,
			"scanned_at": scanned_at or now_datetime(), "transaction_type": transaction_type,
			"source": source, "txn_key": f"{coil}_{frappe.generate_hash(length=8)}",
			"processed": 1, "reversed": reversed,
		}).insert(ignore_permissions=True)
		if sap_status:
			frappe.db.set_value("Coil Transaction", d.name, "sap_status", sap_status,
								update_modified=False)
		frappe.db.commit()
		return d.name

	def _sap(self, name, *fields):
		fields = fields or ("sap_status",)
		v = frappe.db.get_value("Coil Transaction", name, list(fields))
		return v if len(fields) > 1 else v

	def _sweep(self, post=_echo_success):
		"""Run a sweep against a fake SAP; returns (result, list of posted BATCHes)."""
		posted = []

		def fake_post(session, url, data, headers, timeout, verify):
			posted.extend(r["BATCH"] for r in json.loads(data))
			return post(session, url, data, headers, timeout, verify)

		with patch.object(lp, "_csrf_token", return_value="TKN"), \
				patch.object(lp, "_http_post", side_effect=fake_post):
			res = cp.sweep()
		return res, posted

	# ---- payload ----
	def test_build_count_row_from_qr(self):
		s = frappe.get_doc("WMSLite Settings")
		txn = frappe._dict({"coil_barcode": "J2603B111", "coil_qr_raw": FULL_QR, "bin_code": BIN_A,
							"scanned_at": get_datetime("2026-06-27 12:30:45"), "qc": 1})
		row = cp.build_count_row(txn, None, s)
		self.assertEqual(row["TRANSACTIONTYPE"], "T1")
		self.assertEqual(row["PLANT"], "Jangalpur Al Foil")
		self.assertEqual(row["PRODUCT"], "ALUMINIUM FOIL BARE")
		self.assertEqual(row["LOCATION"], BIN_A)            # the counted bin, not a truck
		self.assertEqual(row["BATCH"], "J2603B111")
		self.assertEqual(row["WEIGHT"], "720.500")
		self.assertEqual(row["SIZED"], "6.000 mm X 1280")
		self.assertEqual(row["MATERIAL"], "UL06000")
		self.assertEqual(row["QC"], "QC")                    # SAP's literal for a hold
		self.assertEqual(row["ENTRYDATE"], "27062026")
		self.assertEqual(row["ENTRYTIME"], "123045")
		self.assertEqual(row["USERNAME"], "jindal1")
		self.assertEqual(set(row), {"TRANSACTIONTYPE", "PLANT", "LOCATION", "BATCH", "WEIGHT",
									"PRODUCT", "MATERIAL", "SIZED", "QC", "ENTRYDATE", "ENTRYTIME",
									"USERNAME"})

	def test_build_count_row_fallbacks(self):
		s = frappe.get_doc("WMSLite Settings")
		plain = frappe._dict({"coil_barcode": "__TEST__P1", "coil_qr_raw": "__TEST__P1",
							  "bin_code": BIN_B, "scanned_at": now_datetime(), "qc": 0})
		# Plain barcode + a SAP-fed Bin Inventory row: plant/product come from the bin.
		row = cp.build_count_row(plain, {"plant": "1300", "product": "HR COIL", "weight": 12.5,
										 "sized": "", "material_grade": None}, s)
		self.assertEqual(row["PLANT"], "1300")
		self.assertEqual(row["PRODUCT"], "HR COIL")
		self.assertEqual(row["WEIGHT"], 12.5)
		self.assertEqual(row["MATERIAL"], "NA")              # the Apps Script's fallback
		self.assertEqual(row["QC"], "")
		# Nothing anywhere: the human-readable sentinel, never an empty PLANT.
		row = cp.build_count_row(plain, None, s)
		self.assertEqual(row["PLANT"], lp.NO_SCAN_PLANT)
		self.assertEqual(row["LOCATION"], BIN_B)

	def test_build_count_row_uses_count_txn_type(self):
		_settings(1, txn_type="T9")
		s = frappe.get_doc("WMSLite Settings")
		row = cp.build_count_row(frappe._dict({"coil_barcode": "X", "bin_code": BIN_A}), None, s)
		self.assertEqual(row["TRANSACTIONTYPE"], "T9")

	# ---- arming on scan ----
	def test_scan_with_push_off_parks_disabled(self):
		_settings(0)
		coil = self._coil()
		with patch.object(frappe, "enqueue") as enq:
			count_api.count_scan(BIN_A, coil, client_event_id="cp-off")
		name = frappe.db.get_value("Coil Transaction", {"txn_key": "cp-off"}, "name")
		self.assertEqual(self._sap(name), "Disabled")
		enq.assert_not_called()

	def test_scan_with_push_on_marks_pending_and_enqueues(self):
		coil = self._coil()
		with patch.object(frappe, "enqueue") as enq:
			count_api.count_scan(BIN_A, coil, client_event_id="cp-on")
		name = frappe.db.get_value("Coil Transaction", {"txn_key": "cp-on"}, "name")
		self.assertEqual(self._sap(name), "Pending")
		enq.assert_called_once()
		self.assertEqual(enq.call_args.args[0], cp.SWEEP_JOB)
		self.assertTrue(enq.call_args.kwargs.get("enqueue_after_commit"))

	def test_idempotent_replay_does_not_rearm(self):
		coil = self._coil()
		self._scan(coil, cid="cp-dup")
		with patch.object(frappe, "enqueue") as enq:
			r = count_api.count_scan(BIN_A, coil, client_event_id="cp-dup")
		self.assertTrue(r.get("idempotent"))
		enq.assert_not_called()
		self.assertEqual(frappe.db.count("Coil Transaction", {"coil_barcode": coil}), 1)

	# ---- selection: ONLY live in-app count scans ----
	def test_sweep_selects_only_live_inapp_count_pending(self):
		live = self._scan(self._coil(1))
		feed = self._mktxn(self._coil(2), source="Push API", transaction_type=None)
		rm = self._mktxn(self._coil(3), source="In-app", transaction_type="RM Dispatch")
		blank = self._mktxn(self._coil(4), source="In-app", transaction_type="Inventory Count")
		gone = self._mktxn(self._coil(5), source="In-app", transaction_type="Inventory Count",
						   sap_status="Pending", reversed=1)
		upload = self._mktxn(self._coil(6), source="Upload", transaction_type="Inventory Count",
							 sap_status="Pending")
		res, posted = self._sweep()
		self.assertEqual(posted, [self._coil(1)])
		self.assertEqual(res["sent"], 1)
		self.assertEqual(self._sap(live), "Sent")
		for n in (feed, rm, blank):
			self.assertFalse(self._sap(n))        # NULL or '' — never given a push status
		self.assertEqual(self._sap(gone), "Pending")       # reversed: untouched, never sent
		self.assertEqual(self._sap(upload), "Pending")     # not In-app: untouched

	# ---- outcomes ----
	def test_sweep_sent_path(self):
		a, b = self._scan(self._coil(1)), self._scan(self._coil(2), bin_code=BIN_B)
		res, posted = self._sweep()
		self.assertEqual(res["sent"], 2)
		self.assertEqual(sorted(posted), sorted([self._coil(1), self._coil(2)]))
		for n, bin_code in ((a, BIN_A), (b, BIN_B)):
			st, msg, at, att, req = self._sap(n, "sap_status", "sap_message", "sap_pushed_at",
											  "sap_attempts", "sap_request")
			self.assertEqual(st, "Sent")
			self.assertEqual(msg, SUCCESS)
			self.assertIsNotNone(at)
			self.assertEqual(cint(att), 1)
			self.assertEqual(json.loads(req)["LOCATION"], bin_code)
		logs = frappe.get_all("WMSLite SAP Log", filters={"action": "Inventory Count",
															"payload": ["like", f"%{self._coil(1)}%"]},
							  fields=["result", "direction", "coil_count"])
		self.assertEqual(len(logs), 1)
		self.assertEqual((logs[0].result, logs[0].direction, logs[0].coil_count), ("Accepted", "Outbound", 2))

	def test_sweep_failed_path(self):
		name = self._scan(self._coil())

		def reject(session, url, data, headers, timeout, verify):
			rows = json.loads(data)
			return FakeResp(200, json.dumps([{"BATCH": r["BATCH"], "ENTRYDATE": r["ENTRYDATE"],
											  "ENTRYTIME": r["ENTRYTIME"], "MESSAGE": "Batch not found in SAP"}
											 for r in rows]))
		res, _ = self._sweep(reject)
		self.assertEqual(res["failed"], 1)
		st, msg, att = self._sap(name, "sap_status", "sap_message", "sap_attempts")
		self.assertEqual((st, msg, cint(att)), ("Failed", "Batch not found in SAP", 1))

	def test_sweep_msgtype_s_already_pushed_is_sent(self):
		# SAP's idempotent answer to a re-send (e.g. after a client-side timeout)
		# is MSGTYPE "S" / "Data already Pushed to SAP": the scan IS in SAP.
		name = self._scan(self._coil())

		def already(session, url, data, headers, timeout, verify):
			rows = json.loads(data)
			return FakeResp(200, json.dumps([{"BATCH": r["BATCH"], "ENTRYDATE": r["ENTRYDATE"],
											  "ENTRYTIME": r["ENTRYTIME"], "MSGTYPE": "S",
											  "MESSAGE": "Data already Pushed to SAP"} for r in rows]))
		res, _ = self._sweep(already)
		self.assertEqual(res["sent"], 1)
		st, msg = self._sap(name, "sap_status", "sap_message")
		self.assertEqual((st, msg), ("Sent", "Data already Pushed to SAP"))

	def test_sweep_unechoed_rows_are_resent_one_at_a_time(self):
		# SAP collapses the reply for a batch holding duplicates (seen live
		# 2026-09-23): re-send each unechoed scan alone and take that answer.
		a, b = self._scan(self._coil(1)), self._scan(self._coil(2), bin_code=BIN_B)
		calls = []

		def collapsed(session, url, data, headers, timeout, verify):
			rows = json.loads(data)
			calls.append(len(rows))
			r = rows[-1]
			msg = "Data Pushed Successfully" if len(rows) > 1 else "Data already Pushed to SAP"
			return FakeResp(200, json.dumps([{"BATCH": r["BATCH"], "ENTRYDATE": r["ENTRYDATE"],
											  "ENTRYTIME": r["ENTRYTIME"], "MSGTYPE": "S", "MESSAGE": msg}]))
		res, _ = self._sweep(collapsed)
		self.assertEqual(res["sent"], 2)
		self.assertEqual(calls, [2, 1])
		self.assertEqual(self._sap(a), "Sent")
		self.assertEqual(self._sap(b), "Sent")

	def test_sweep_transport_error_keeps_pending(self):
		name = self._scan(self._coil())
		with patch.object(lp, "_csrf_token", side_effect=ValueError("CSRF fetch failed")):
			res = cp.sweep()
		self.assertEqual(res["deferred"], 1)
		st, att = self._sap(name, "sap_status", "sap_attempts")
		self.assertEqual((st, cint(att)), ("Pending", 0))
		logs = frappe.get_all("WMSLite SAP Log", filters={"action": "Inventory Count",
															"payload": ["like", f"%{self._coil()}%"]},
							  pluck="result")
		self.assertEqual(logs, ["Error"])

	def test_sweep_http_500_keeps_pending(self):
		name = self._scan(self._coil())
		res, _ = self._sweep(lambda *a: FakeResp(503, "Service Unavailable"))
		self.assertEqual(res["deferred"], 1)
		st, att = self._sap(name, "sap_status", "sap_attempts")
		self.assertEqual((st, cint(att)), ("Pending", 0))

	# ---- claim / recovery ----
	def test_claim_skips_rows_already_sending(self):
		name = self._scan(self._coil())
		frappe.db.set_value("Coil Transaction", name, {"sap_status": "Sending",
													   "sap_pushed_at": now_datetime()},
							update_modified=False)
		frappe.db.commit()
		res, posted = self._sweep()
		self.assertEqual(posted, [])
		self.assertEqual(res, {"pending": 0})
		self.assertEqual(self._sap(name), "Sending")

	def test_stuck_sending_is_recovered_and_sent(self):
		name = self._scan(self._coil())
		frappe.db.set_value("Coil Transaction", name,
							{"sap_status": "Sending",
							 "sap_pushed_at": add_to_date(now_datetime(), minutes=-30)},
							update_modified=False)
		frappe.db.commit()
		res, posted = self._sweep()
		self.assertEqual(posted, [self._coil()])
		self.assertEqual(self._sap(name), "Sent")

	def test_sweep_disabled_noop(self):
		name = self._scan(self._coil())          # armed while ON ...
		_settings(0)                             # ... then the switch goes OFF
		res, posted = self._sweep()
		self.assertEqual(res, {"skipped": "disabled"})
		self.assertEqual(posted, [])
		self.assertEqual(self._sap(name), "Pending")

	# ---- undo on the handheld ----
	def test_remove_after_sent_keeps_row_reversed_with_note(self):
		coil = self._coil()
		name = self._scan(coil, cid="cp-rm-sent")
		self._sweep()
		self.assertEqual(self._sap(name), "Sent")

		res, posted = None, []
		with patch.object(lp, "_csrf_token", return_value="TKN"), \
				patch.object(lp, "_http_post", side_effect=_echo_success) as post:
			count_api.count_remove(BIN_A, coil, client_event_id="cp-rm-sent")
			post.assert_not_called()             # SAP is never told about an undo

		self.assertTrue(frappe.db.exists("Coil Transaction", name))   # kept, not deleted
		rev, by, st, msg = self._sap(name, "reversed", "reversed_by", "sap_status", "sap_message")
		self.assertEqual((cint(rev), by, st), (1, "Administrator", "Sent"))
		self.assertIn(cp.REMOVED_NOTE, msg)
		self.assertIn(SUCCESS, msg)
		# Brand-new coil with no prior location: its Bin Inventory row is dropped.
		self.assertFalse(frappe.db.exists("Bin Inventory", coil))

	def test_remove_before_send_marks_not_required_and_is_never_swept(self):
		coil = self._coil()
		name = self._scan(coil, cid="cp-rm-pend")
		count_api.count_remove(BIN_A, coil, client_event_id="cp-rm-pend")
		self.assertEqual(self._sap(name), "Not Required")
		res, posted = self._sweep()
		self.assertEqual(posted, [])

	def test_remove_twice_is_idempotent(self):
		coil = self._coil()
		name = self._scan(coil, cid="cp-rm-twice")
		count_api.count_remove(BIN_A, coil, client_event_id="cp-rm-twice")
		first = self._sap(name, "reversed_at", "sap_status", "sap_message")
		count_api.count_remove(BIN_A, coil, client_event_id="cp-rm-twice")
		self.assertEqual(self._sap(name, "reversed_at", "sap_status", "sap_message"), first)
		self.assertFalse(frappe.db.exists("Bin Inventory", coil))

	def test_rederive_ignores_reversed_rows(self):
		coil = self._coil()
		self._scan(coil, bin_code=BIN_A)                     # older, live
		later = self._mktxn(coil, source="In-app", transaction_type="Inventory Count",
							bin_code=BIN_B, reversed=1,
							scanned_at=add_to_date(now_datetime(), minutes=5))
		count_api._rederive_coil(coil)
		self.assertEqual(frappe.db.get_value("Bin Inventory", coil, "bin_code"), BIN_A)
		self.assertTrue(frappe.db.exists("Coil Transaction", later))

	# ---- controls ----
	def test_requeue_only_failed(self):
		failed = self._scan(self._coil(1))
		sent = self._scan(self._coil(2))
		frappe.db.set_value("Coil Transaction", failed, "sap_status", "Failed", update_modified=False)
		frappe.db.set_value("Coil Transaction", sent, "sap_status", "Sent", update_modified=False)
		gone = self._mktxn(self._coil(3), source="In-app", transaction_type="Inventory Count",
						   sap_status="Failed", reversed=1)
		frappe.db.commit()
		with patch.object(frappe, "enqueue") as enq:
			self.assertEqual(cp.requeue(failed), {"ok": True})
			self.assertEqual(cp.requeue(sent), {"skipped": "Sent"})
			self.assertEqual(cp.requeue(gone), {"skipped": "removed in WMS"})
		enq.assert_called_once()
		self.assertEqual(self._sap(failed), "Pending")
		self.assertEqual(self._sap(sent), "Sent")
		_settings(0)
		self.assertEqual(cp.requeue(failed), {"skipped": "disabled"})

	def test_trigger_push_guards(self):
		disabled = self._scan(self._coil(1))
		sent = self._scan(self._coil(2))
		frappe.db.set_value("Coil Transaction", disabled, "sap_status", "Disabled", update_modified=False)
		frappe.db.set_value("Coil Transaction", sent, "sap_status", "Sent", update_modified=False)
		feed = self._mktxn(self._coil(3), source="Push API")
		gone = self._mktxn(self._coil(4), source="In-app", transaction_type="Inventory Count",
						   sap_status="Pending", reversed=1)
		frappe.db.commit()
		with patch.object(frappe, "enqueue") as enq:
			self.assertEqual(cp.trigger_push(disabled), {"ok": True})
			self.assertEqual(cp.trigger_push(sent), {"skipped": "Sent"})
			self.assertIn("not an in-app", cp.trigger_push(feed)["skipped"])
			self.assertIn("removed", cp.trigger_push(gone)["skipped"])
		enq.assert_called_once()
		self.assertEqual(self._sap(disabled), "Pending")
		self.assertFalse(self._sap(feed))

	def test_push_disabled_arms_only_live_disabled_rows(self):
		_settings(0)
		live = self._scan(self._coil(1))                       # parks at Disabled
		self.assertEqual(self._sap(live), "Disabled")
		gone = self._mktxn(self._coil(2), source="In-app", transaction_type="Inventory Count",
						   sap_status="Disabled", reversed=1)
		_settings(1)
		with patch.object(frappe, "enqueue") as enq:
			self.assertEqual(cp.push_disabled(), {"armed": 1})
		enq.assert_called_once()
		self.assertEqual(self._sap(live), "Pending")
		self.assertEqual(self._sap(gone), "Disabled")

	# ---- offline replay of an undone scan ----
	def test_offline_replay_of_reversed_scan_is_noop(self):
		coil = self._coil()
		self._scan(coil, cid="cp-off-rev")
		count_api.count_remove(BIN_A, coil, client_event_id="cp-off-rev")
		with patch.object(frappe, "enqueue") as enq:
			results = api.submit_offline_queue(json.dumps([
				{"type": "count_scan", "bin_code": BIN_A, "raw_qr": coil, "coil_barcode": coil,
				 "qc": 0, "client_event_id": "cp-off-rev"}]))
		self.assertTrue(results[0].get("ok"))
		enq.assert_not_called()
		self.assertEqual(frappe.db.count("Coil Transaction", {"txn_key": "cp-off-rev"}), 1)
		self.assertEqual(self._sap(frappe.db.get_value("Coil Transaction", {"txn_key": "cp-off-rev"}, "name")),
						 "Not Required")
		self.assertFalse(frappe.db.exists("Bin Inventory", coil))
