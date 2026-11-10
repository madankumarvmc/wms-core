"""Unit tests for the outbound SAP T2 push of RM line receipts (mocked HTTP).

The CSRF/HTTP seams on loading_push are mocked (rm_push.post_rows resolves them
there), so these tests NEVER call a real SAP endpoint. All data is __TEST__-
prefixed and torn down; every WMSLite Settings field touched is restored.

    bench --site <site> run-tests --app sbx_wmslite --module sbx_wmslite.tests.test_rm_push
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from sbx_wmslite import bin_api, console_api, rm_api
from sbx_wmslite import count_push as cp
from sbx_wmslite import loading_push as lp
from sbx_wmslite import rm_push as rp
from sbx_wmslite.tests.settings_guard import SettingsGuard, assert_no_live_pending_pushes

LINE_A = "__TEST__RPLINE_A"
LINE_B = "__TEST__RPLINE_B"
YARD = "__TEST__RPYARD"
SUCCESS = "Data Pushed Successfully"
FULL_QR = "Jangalpur Al Foil - ALUMINIUM FOIL BARE-J2603B111 - 6.000 mm  X 1280 - 720.500 KG- UL06000"


class FakeResp:
	def __init__(self, status_code, text):
		self.status_code = status_code
		self.text = text


# Every field these tests write is listed here so tearDownClass can put the
# site's own values back — this Single is shared with the live site.
SETTINGS = SettingsGuard(
	"rm_enabled",
	"rm_unknown_bin",
	"rm_push_enabled",
	"rm_txn_type",
	"count_push_enabled",
	"loading_push_url",
	"loading_push_user",
	"loading_push_batch_size",
	"verify_tls",
	"qr_decode_rule",
	"bin_decode_rule",
	"bin_validation_enabled",
)


def _settings(push=1, txn_type="T2", count_push=0):
	SETTINGS.apply(
		rm_enabled=1,
		rm_unknown_bin="__TEST__RP_UNKNOWN",
		rm_push_enabled=push,
		rm_txn_type=txn_type,
		count_push_enabled=count_push,
		loading_push_url="https://sap.example.invalid/sap/bc/zpwmsb/rest",
		loading_push_user="jindal1",
		loading_push_batch_size=200,
		verify_tls=0,
		# Identity decode: the scanned string IS the coil id. __TEST__ lines are not
		# in the location master, so validation is off (covered in test_locations).
		qr_decode_rule="Identity", bin_decode_rule="Identity", bin_validation_enabled=0,
	)


def _cleanup():
	for dt, field in (("Coil Transaction", "coil_barcode"), ("Bin Inventory", "coil_barcode")):
		for n in frappe.get_all(dt, filters={field: ["like", "__TEST__%"]}, pluck="name"):
			frappe.delete_doc(dt, n, force=1, ignore_permissions=True)
	for n in frappe.get_all("WMSLite SAP Log", filters={"action": ["in", ("RM Receipt", "Inventory Count")],
														 "payload": ["like", "%__TEST__%"]}, pluck="name"):
		frappe.delete_doc("WMSLite SAP Log", n, force=1, ignore_permissions=True)
	frappe.db.commit()


def _echo_success(session, url, data, headers, timeout, verify):
	rows = json.loads(data)
	out = [{"BATCH": r["BATCH"], "ENTRYDATE": r["ENTRYDATE"], "ENTRYTIME": r["ENTRYTIME"],
			"MESSAGE": SUCCESS} for r in rows]
	return FakeResp(200, json.dumps(out))


class TestRMPush(FrappeTestCase):
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
		# sweep() is site-wide and SAP is mocked below: a real Pending receipt would
		# be marked Sent by the echo. Refuse rather than corrupt (see settings_guard).
		assert_no_live_pending_pushes()
		frappe.set_user("Administrator")
		_cleanup()
		_settings(1)

	# ---- helpers ----
	def _coil(self, n=1):
		return "__TEST__RP_" + self._testMethodName + "_" + str(n)

	def _seed(self, coil, **kw):
		"""A coil the SAP feed already knows about, sitting in a warehouse bin."""
		data = {"bin_code": YARD, "material_grade": "X52", "weight": 4200, "plant": "1300",
				"product": "HR COIL"}
		data.update(kw)
		bin_api.upsert_bin(coil, data, source="Push API")
		frappe.db.commit()

	def _dispatch(self, coil, line=LINE_A, cid=None):
		with patch.object(frappe, "enqueue"):
			rm_api.rm_dispatch(line, coil, client_event_id=cid)

	def _receive(self, coil, line=LINE_A, cid=None, seed=True, **kw):
		"""Dispatch + receive with the sweep enqueue swallowed; returns the receipt
		row's name."""
		if seed:
			self._seed(coil)
		self._dispatch(coil, line)
		with patch.object(frappe, "enqueue"):
			rm_api.rm_receive(line, coil, client_event_id=cid, **kw)
		return self._receipt(coil)

	def _receipt(self, coil):
		return frappe.db.get_value("Coil Transaction", {"coil_barcode": coil, "reversed": 0,
														 "transaction_type": rm_api.TXN_RECEIPT}, "name")

	def _mktxn(self, coil, source="In-app", transaction_type=None, sap_status=None,
			   reversed=0, scanned_at=None):
		"""A raw Coil Transaction of any population (feed / upload / RM / count)."""
		d = frappe.get_doc({
			"doctype": "Coil Transaction", "coil_barcode": coil, "bin_code": YARD,
			"scanned_at": scanned_at or now_datetime(), "transaction_type": transaction_type,
			"source": source, "txn_key": f"{coil}_{frappe.generate_hash(length=8)}",
			"rm_destination": LINE_A, "processed": 1, "reversed": reversed,
		}).insert(ignore_permissions=True)
		if sap_status:
			frappe.db.set_value("Coil Transaction", d.name, "sap_status", sap_status,
								update_modified=False)
		frappe.db.commit()
		return d.name

	def _sap(self, name, *fields):
		fields = fields or ("sap_status",)
		return frappe.db.get_value("Coil Transaction", name, list(fields))

	def _sweep(self, post=_echo_success, mod=rp):
		"""Run a sweep against a fake SAP; returns (result, list of posted BATCHes)."""
		posted = []

		def fake_post(session, url, data, headers, timeout, verify):
			posted.extend(r["BATCH"] for r in json.loads(data))
			return post(session, url, data, headers, timeout, verify)

		with patch.object(lp, "_csrf_token", return_value="TKN"), \
				patch.object(lp, "_http_post", side_effect=fake_post):
			res = mod.sweep()
		return res, posted

	# ---- payload ----
	def test_build_rm_row_from_qr(self):
		s = frappe.get_doc("WMSLite Settings")
		txn = frappe._dict({"coil_barcode": "J2603B111", "coil_qr_raw": FULL_QR,
							"rm_destination": LINE_A,
							"scanned_at": get_datetime("2026-10-08 09:15:30")})
		row = rp.build_rm_row(txn, {"qc": 1}, s)
		self.assertEqual(row["TRANSACTIONTYPE"], "T2")
		self.assertEqual(row["PLANT"], "Jangalpur Al Foil")
		self.assertEqual(row["PRODUCT"], "ALUMINIUM FOIL BARE")
		self.assertEqual(row["LOCATION"], LINE_A)            # the line, not a bin or truck
		self.assertEqual(row["BATCH"], "J2603B111")
		self.assertEqual(row["WEIGHT"], "720.500")
		self.assertEqual(row["SIZED"], "6.000 mm X 1280")
		self.assertEqual(row["MATERIAL"], "UL06000")
		self.assertEqual(row["QC"], "QC")                    # hold flag read off the coil
		self.assertEqual(row["ENTRYDATE"], "08102026")
		self.assertEqual(row["ENTRYTIME"], "091530")
		self.assertEqual(row["USERNAME"], "jindal1")
		self.assertEqual(set(row), {"TRANSACTIONTYPE", "PLANT", "LOCATION", "BATCH", "WEIGHT",
									"PRODUCT", "MATERIAL", "SIZED", "QC", "ENTRYDATE", "ENTRYTIME",
									"USERNAME"})

	def test_build_rm_row_fallbacks(self):
		s = frappe.get_doc("WMSLite Settings")
		plain = frappe._dict({"coil_barcode": "__TEST__P1", "coil_qr_raw": "__TEST__P1",
							  "rm_destination": LINE_B, "scanned_at": now_datetime()})
		# Plain barcode + a SAP-fed Bin Inventory row: plant/product come from the coil.
		row = rp.build_rm_row(plain, {"plant": "1300", "product": "HR COIL", "weight": 12.5,
									  "sized": "", "material_grade": None, "qc": 0}, s)
		self.assertEqual(row["PLANT"], "1300")
		self.assertEqual(row["PRODUCT"], "HR COIL")
		self.assertEqual(row["WEIGHT"], 12.5)
		self.assertEqual(row["MATERIAL"], "NA")              # the Apps Script's fallback
		self.assertEqual(row["QC"], "")
		self.assertEqual(row["LOCATION"], LINE_B)
		# Nothing anywhere: the human-readable sentinel, never an empty PLANT.
		row = rp.build_rm_row(plain, None, s)
		self.assertEqual(row["PLANT"], lp.NO_SCAN_PLANT)

	def test_build_rm_row_uses_rm_txn_type(self):
		_settings(1, txn_type="T7")
		s = frappe.get_doc("WMSLite Settings")
		row = rp.build_rm_row(frappe._dict({"coil_barcode": "X", "rm_destination": LINE_A}), None, s)
		self.assertEqual(row["TRANSACTIONTYPE"], "T7")

	# ---- arming on receipt ----
	def test_receive_with_push_off_parks_disabled(self):
		_settings(0)
		coil = self._coil()
		self._seed(coil)
		self._dispatch(coil)
		with patch.object(frappe, "enqueue") as enq:
			rm_api.rm_receive(LINE_A, coil, client_event_id="rp-off")
		name = frappe.db.get_value("Coil Transaction", {"txn_key": "rp-off"}, "name")
		self.assertEqual(self._sap(name), "Disabled")
		enq.assert_not_called()

	def test_receive_with_push_on_marks_pending_and_enqueues(self):
		coil = self._coil()
		self._seed(coil)
		self._dispatch(coil)
		with patch.object(frappe, "enqueue") as enq:
			rm_api.rm_receive(LINE_A, coil, client_event_id="rp-on")
		name = frappe.db.get_value("Coil Transaction", {"txn_key": "rp-on"}, "name")
		self.assertEqual(self._sap(name), "Pending")
		enq.assert_called_once()
		self.assertEqual(enq.call_args.args[0], rp.SWEEP_JOB)
		self.assertTrue(enq.call_args.kwargs.get("enqueue_after_commit"))

	def test_dispatch_is_never_armed(self):
		coil = self._coil()
		self._seed(coil)
		with patch.object(frappe, "enqueue") as enq:
			rm_api.rm_dispatch(LINE_A, coil, client_event_id="rp-d")
		enq.assert_not_called()
		name = frappe.db.get_value("Coil Transaction", {"txn_key": "rp-d"}, "name")
		self.assertFalse(self._sap(name))                    # NULL / '' — never queued

	def test_idempotent_replay_does_not_rearm(self):
		coil = self._coil()
		self._receive(coil, cid="rp-dup")
		with patch.object(frappe, "enqueue") as enq:
			r = rm_api.rm_receive(LINE_A, coil, client_event_id="rp-dup")
		self.assertTrue(r.get("idempotent"))
		enq.assert_not_called()
		self.assertEqual(frappe.db.count("Coil Transaction",
										 {"coil_barcode": coil, "transaction_type": rm_api.TXN_RECEIPT}), 1)

	def test_re_receive_at_same_line_does_not_rearm(self):
		coil = self._coil()
		name = self._receive(coil, cid="rp-again-1")
		frappe.db.set_value("Coil Transaction", name, "sap_status", "Sent", update_modified=False)
		frappe.db.commit()
		with patch.object(frappe, "enqueue") as enq:
			r = rm_api.rm_receive(LINE_A, coil, client_event_id="rp-again-2")
		self.assertTrue(r.get("already_received"))
		enq.assert_not_called()
		self.assertEqual(self._sap(name), "Sent")

	def test_flagged_offline_receipt_is_still_pushed(self):
		# Never dispatched, replayed from an offline device: accepted + flagged, and
		# SAP is told — the coil is physically at the line.
		coil = self._coil()
		self._seed(coil)
		with patch.object(frappe, "enqueue") as enq:
			r = rm_api.rm_receive(LINE_A, coil, client_event_id="rp-flag", synced_offline=1)
		self.assertEqual(r.get("unplanned"), 1)
		enq.assert_called_once()
		name = self._receipt(coil)
		self.assertEqual(self._sap(name), "Pending")
		res, posted = self._sweep()
		self.assertEqual(posted, [coil])
		self.assertEqual(json.loads(self._sap(name, "sap_request"))["LOCATION"], LINE_A)

	# ---- selection: ONLY live in-app receipts ----
	def test_sweep_selects_only_live_receipts(self):
		live = self._receive(self._coil(1))
		dispatch = frappe.db.get_value("Coil Transaction",
									   {"coil_barcode": self._coil(1), "transaction_type": rm_api.TXN_DISPATCH}, "name")
		count = self._mktxn(self._coil(2), transaction_type="Inventory Count", sap_status="Pending")
		reversal = self._mktxn(self._coil(3), transaction_type=rm_api.TXN_RECEIPT_REVERSAL)
		gone = self._mktxn(self._coil(4), transaction_type=rm_api.TXN_RECEIPT,
						   sap_status="Pending", reversed=1)
		upload = self._mktxn(self._coil(5), source="Upload", transaction_type=rm_api.TXN_RECEIPT,
							 sap_status="Pending")
		feed = self._mktxn(self._coil(6), source="Push API", transaction_type=None)
		res, posted = self._sweep()
		self.assertEqual(posted, [self._coil(1)])
		self.assertEqual(res["sent"], 1)
		self.assertEqual(self._sap(live), "Sent")
		for n in (dispatch, reversal, feed):
			self.assertFalse(self._sap(n))               # never given a push status
		self.assertEqual(self._sap(count), "Pending")      # the count push's row, not ours
		self.assertEqual(self._sap(gone), "Pending")       # reversed: untouched, never sent
		self.assertEqual(self._sap(upload), "Pending")     # not In-app: untouched

	def test_count_sweep_ignores_pending_receipts(self):
		# Cross-isolation the other way: the T1 count sweep never takes a receipt.
		_settings(1, count_push=1)
		name = self._receive(self._coil())
		res, posted = self._sweep(mod=cp)
		self.assertEqual(posted, [])
		self.assertEqual(self._sap(name), "Pending")

	# ---- outcomes ----
	def test_sweep_sent_path(self):
		a = self._receive(self._coil(1), line=LINE_A)
		b = self._receive(self._coil(2), line=LINE_B)
		res, posted = self._sweep()
		self.assertEqual(res["sent"], 2)
		self.assertEqual(sorted(posted), sorted([self._coil(1), self._coil(2)]))
		for n, line in ((a, LINE_A), (b, LINE_B)):
			st, msg, at, att, req = self._sap(n, "sap_status", "sap_message", "sap_pushed_at",
											  "sap_attempts", "sap_request")
			self.assertEqual(st, "Sent")
			self.assertEqual(msg, SUCCESS)
			self.assertIsNotNone(at)
			self.assertEqual(cint(att), 1)
			req = json.loads(req)
			self.assertEqual(req["LOCATION"], line)
			self.assertEqual(req["TRANSACTIONTYPE"], "T2")
			self.assertEqual(req["PLANT"], "1300")            # off the seeded coil
			self.assertEqual(req["MATERIAL"], "X52")
		logs = frappe.get_all("WMSLite SAP Log", filters={"action": "RM Receipt",
															"payload": ["like", f"%{self._coil(1)}%"]},
							  fields=["result", "direction", "coil_count"])
		self.assertEqual(len(logs), 1)
		self.assertEqual((logs[0].result, logs[0].direction, logs[0].coil_count), ("Accepted", "Outbound", 2))

	def test_sweep_failed_path(self):
		name = self._receive(self._coil())

		def reject(session, url, data, headers, timeout, verify):
			rows = json.loads(data)
			return FakeResp(200, json.dumps([{"BATCH": r["BATCH"], "ENTRYDATE": r["ENTRYDATE"],
											  "ENTRYTIME": r["ENTRYTIME"], "MESSAGE": "Batch not found in SAP"}
											 for r in rows]))
		res, _ = self._sweep(reject)
		self.assertEqual(res["failed"], 1)
		st, msg, att = self._sap(name, "sap_status", "sap_message", "sap_attempts")
		self.assertEqual((st, msg, cint(att)), ("Failed", "Batch not found in SAP", 1))

	def test_sweep_transport_error_keeps_pending(self):
		name = self._receive(self._coil())
		with patch.object(lp, "_csrf_token", side_effect=ValueError("CSRF fetch failed")):
			res = rp.sweep()
		self.assertEqual(res["deferred"], 1)
		st, att = self._sap(name, "sap_status", "sap_attempts")
		self.assertEqual((st, cint(att)), ("Pending", 0))
		logs = frappe.get_all("WMSLite SAP Log", filters={"action": "RM Receipt",
															"payload": ["like", f"%{self._coil()}%"]},
							  pluck="result")
		self.assertEqual(logs, ["Error"])

	def test_sweep_http_500_keeps_pending(self):
		name = self._receive(self._coil())
		res, _ = self._sweep(lambda *a: FakeResp(503, "Service Unavailable"))
		self.assertEqual(res["deferred"], 1)
		st, att = self._sap(name, "sap_status", "sap_attempts")
		self.assertEqual((st, cint(att)), ("Pending", 0))

	def test_stuck_sending_is_recovered_and_sent(self):
		name = self._receive(self._coil())
		frappe.db.set_value("Coil Transaction", name,
							{"sap_status": "Sending",
							 "sap_pushed_at": add_to_date(now_datetime(), minutes=-30)},
							update_modified=False)
		frappe.db.commit()
		res, posted = self._sweep()
		self.assertEqual(posted, [self._coil()])
		self.assertEqual(self._sap(name), "Sent")

	def test_sweep_disabled_noop(self):
		name = self._receive(self._coil())        # armed while ON ...
		_settings(0)                              # ... then the switch goes OFF
		res, posted = self._sweep()
		self.assertEqual(res, {"skipped": "disabled"})
		self.assertEqual(posted, [])
		self.assertEqual(self._sap(name), "Pending")

	# ---- undo on the handheld: like the count push, SAP is never told ----
	def test_undo_after_sent_keeps_row_reversed_with_note(self):
		coil = self._coil()
		name = self._receive(coil, cid="rp-undo-sent")
		self._sweep()
		self.assertEqual(self._sap(name), "Sent")

		with patch.object(lp, "_csrf_token", return_value="TKN"), \
				patch.object(lp, "_http_post", side_effect=_echo_success) as post, \
				patch.object(frappe, "enqueue") as enq:
			rm_api.rm_remove(coil, "receive", client_event_id="rp-undo-sent")
			post.assert_not_called()              # SAP is never told about an undo
			enq.assert_not_called()

		self.assertTrue(frappe.db.exists("Coil Transaction", name))   # kept, not deleted
		rev, by, st, msg = self._sap(name, "reversed", "reversed_by", "sap_status", "sap_message")
		self.assertEqual((cint(rev), by, st), (1, "Administrator", "Sent"))
		self.assertIn(rp.REMOVED_NOTE, msg)
		self.assertIn(SUCCESS, msg)
		# The WMS side is back where it was: in transit, with the warehouse bin.
		self.assertEqual(frappe.db.get_value("Bin Inventory", coil, "rm_status"), rm_api.IN_TRANSIT)
		self.assertEqual(frappe.db.get_value("Bin Inventory", coil, "bin_code"), YARD)
		# The reversal row itself is not a push candidate.
		rev_row = frappe.db.get_value("Coil Transaction",
									  {"coil_barcode": coil, "transaction_type": rm_api.TXN_RECEIPT_REVERSAL},
									  "sap_status")
		self.assertFalse(rev_row)

	def test_undo_before_send_marks_not_required_and_is_never_swept(self):
		coil = self._coil()
		name = self._receive(coil, cid="rp-undo-pend")
		with patch.object(frappe, "enqueue"):
			rm_api.rm_remove(coil, "receive", client_event_id="rp-undo-pend")
		self.assertEqual(self._sap(name), "Not Required")
		res, posted = self._sweep()
		self.assertEqual(posted, [])

	def test_undo_dispatch_touches_no_push_state(self):
		coil = self._coil()
		self._seed(coil)
		self._dispatch(coil, cid="rp-undo-d")
		with patch.object(frappe, "enqueue") as enq:
			rm_api.rm_remove(coil, "dispatch", client_event_id="rp-undo-d")
		enq.assert_not_called()
		for n in frappe.get_all("Coil Transaction", filters={"coil_barcode": coil}, pluck="name"):
			self.assertFalse(self._sap(n))

	def test_receive_after_undo_is_a_fresh_push(self):
		# Undo (Sent, kept) then receive again: the new receipt row is its own
		# Pending push; the old one stays Sent + noted.
		coil = self._coil()
		old = self._receive(coil, cid="rp-re-1")
		self._sweep()
		with patch.object(frappe, "enqueue"):
			rm_api.rm_remove(coil, "receive", client_event_id="rp-re-1")
			rm_api.rm_receive(LINE_A, coil, client_event_id="rp-re-2")
		new = self._receipt(coil)
		self.assertNotEqual(old, new)
		self.assertEqual(self._sap(old), "Sent")
		self.assertEqual(self._sap(new), "Pending")
		res, posted = self._sweep()
		self.assertEqual(posted, [coil])
		self.assertEqual(self._sap(new), "Sent")

	# ---- controls ----
	def test_requeue_only_failed(self):
		failed = self._receive(self._coil(1))
		sent = self._receive(self._coil(2))
		frappe.db.set_value("Coil Transaction", failed, "sap_status", "Failed", update_modified=False)
		frappe.db.set_value("Coil Transaction", sent, "sap_status", "Sent", update_modified=False)
		gone = self._mktxn(self._coil(3), transaction_type=rm_api.TXN_RECEIPT,
						   sap_status="Failed", reversed=1)
		count = self._mktxn(self._coil(4), transaction_type="Inventory Count", sap_status="Failed")
		frappe.db.commit()
		with patch.object(frappe, "enqueue") as enq:
			self.assertEqual(rp.requeue(failed), {"ok": True})
			self.assertEqual(rp.requeue(sent), {"skipped": "Sent"})
			self.assertEqual(rp.requeue(gone), {"skipped": "undone in WMS"})
			self.assertEqual(rp.requeue(count), {"skipped": "not an RM receipt"})
		enq.assert_called_once()
		self.assertEqual(self._sap(failed), "Pending")
		self.assertEqual(self._sap(sent), "Sent")
		_settings(0)
		self.assertEqual(rp.requeue(failed), {"skipped": "disabled"})

	def test_console_retry_routes_by_transaction_type(self):
		receipt = self._receive(self._coil(1))
		count = self._mktxn(self._coil(2), transaction_type="Inventory Count", sap_status="Failed")
		frappe.db.set_value("Coil Transaction", receipt, "sap_status", "Failed", update_modified=False)
		frappe.db.commit()
		with patch.object(frappe, "enqueue") as enq, patch.object(cp, "requeue", return_value={"ok": True}) as cq:
			self.assertEqual(console_api.retry_sap_push(receipt), {"ok": True})
			cq.assert_not_called()
			self.assertEqual(console_api.retry_sap_push(count), {"ok": True})
			cq.assert_called_once_with(count)
		enq.assert_called_once()
		self.assertEqual(self._sap(receipt), "Pending")

	def test_trigger_push_guards(self):
		disabled = self._receive(self._coil(1))
		sent = self._receive(self._coil(2))
		frappe.db.set_value("Coil Transaction", disabled, "sap_status", "Disabled", update_modified=False)
		frappe.db.set_value("Coil Transaction", sent, "sap_status", "Sent", update_modified=False)
		dispatch = frappe.db.get_value("Coil Transaction",
									   {"coil_barcode": self._coil(1), "transaction_type": rm_api.TXN_DISPATCH}, "name")
		gone = self._mktxn(self._coil(3), transaction_type=rm_api.TXN_RECEIPT,
						   sap_status="Pending", reversed=1)
		frappe.db.commit()
		with patch.object(frappe, "enqueue") as enq:
			self.assertEqual(rp.trigger_push(disabled), {"ok": True})
			self.assertEqual(rp.trigger_push(sent), {"skipped": "Sent"})
			self.assertIn("not an RM", rp.trigger_push(dispatch)["skipped"])
			self.assertIn("undone", rp.trigger_push(gone)["skipped"])
		enq.assert_called_once()
		self.assertEqual(self._sap(disabled), "Pending")
		self.assertFalse(self._sap(dispatch))

	def test_push_disabled_arms_only_live_disabled_rows(self):
		_settings(0)
		live = self._receive(self._coil(1))                    # parks at Disabled
		self.assertEqual(self._sap(live), "Disabled")
		gone = self._mktxn(self._coil(2), transaction_type=rm_api.TXN_RECEIPT,
						   sap_status="Disabled", reversed=1)
		_settings(1)
		with patch.object(frappe, "enqueue") as enq:
			self.assertEqual(rp.push_disabled(), {"armed": 1})
		enq.assert_called_once()
		self.assertEqual(self._sap(live), "Pending")
		self.assertEqual(self._sap(gone), "Disabled")
