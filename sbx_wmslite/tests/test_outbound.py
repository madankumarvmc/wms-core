"""Tests for the outbound SAP goods-issue confirmation flow.

HTTP + OAuth token are mocked; frappe.enqueue is stubbed so sends are driven
explicitly and deterministically. Data is __TEST__-namespaced.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from sbx_wmslite import api, sap_api, outbound_sap, console_api

TRUCK = "__TEST__OUT"


class FakeResp:
	def __init__(self, status, body=None):
		self.status_code = status
		self._body = body or {}
		self.text = json.dumps(self._body)

	def json(self):
		return self._body


def _cleanup():
	for n in frappe.get_all("Loading Plan", filters={"truck_number": ["like", "__TEST__%"]}, pluck="name"):
		frappe.delete_doc("Loading Plan", n, force=1, ignore_permissions=True)
	for n in frappe.get_all("WMSLite SAP Confirmation", filters={"truck_number": ["like", "__TEST__%"]}, pluck="name"):
		frappe.delete_doc("WMSLite SAP Confirmation", n, force=1, ignore_permissions=True)
	# Coil Load Events carry the unique client_event_id; must be purged too or a
	# reused id makes confirm_load short-circuit as idempotent in the next test.
	for n in frappe.get_all("Coil Load Event", filters={"truck_number": ["like", "__TEST__%"]}, pluck="name"):
		frappe.delete_doc("Coil Load Event", n, force=1, ignore_permissions=True)
	frappe.db.commit()


class TestOutboundSAP(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def setUp(self):
		_cleanup()
		self.tag = TRUCK + "_" + self._testMethodName
		s = frappe.get_doc("WMSLite Settings")
		s.gi_enabled = 1
		s.sap_token_url = "https://sap.example/token"
		s.sap_client_id = "cid"
		s.sap_client_secret = "secret"
		s.sap_gi_url = "https://sap.example/gi"
		s.verify_tls = 0
		s.gi_max_attempts = 2
		s.gi_auto_send_full = 1
		s.gi_hold_short_for_review = 1
		s.supervisor_pin = "1111"
		s.save(ignore_permissions=True)
		frappe.db.commit()

		# mock token + http + enqueue
		self._orig = (outbound_sap._get_token, outbound_sap._http_post, frappe.enqueue)
		self._resp = FakeResp(200, {"gi_document": "DOC-1"})
		outbound_sap._get_token = lambda *a, **k: "tok"
		outbound_sap._http_post = lambda *a, **k: self._resp
		frappe.enqueue = lambda *a, **k: None

	def tearDown(self):
		outbound_sap._get_token, outbound_sap._http_post, frappe.enqueue = self._orig
		frappe.db.set_single_value("WMSLite Settings", "gi_enabled", 0)
		frappe.db.commit()
		_cleanup()

	def _plan(self, coils):
		return sap_api._upsert(None, self.tag, self.tag, coils, {"sap_plan_id": self.tag})["plan"]

	def _conf(self, plan):
		return frappe.db.get_value("WMSLite SAP Confirmation", {"loading_plan": plan},
								   ["name", "status"], as_dict=True)

	def test_full_completion_creates_pending_then_confirmed(self):
		p = self._plan([{"coil_barcode": "A"}, {"coil_barcode": "B"}])
		api.confirm_load(p, "A", client_event_id="a")
		api.confirm_load(p, "B", client_event_id="b")  # -> Completed -> on_plan_completed
		conf = self._conf(p)
		self.assertEqual(conf.status, "Pending")
		r = outbound_sap.send(conf.name)
		self.assertEqual(r["status"], "Confirmed")
		self.assertEqual(r["gi_document"], "DOC-1")
		self.assertEqual(frappe.db.get_value("Loading Plan", p, "gi_status"), "Confirmed")

	def test_only_one_confirmation_per_plan(self):
		p = self._plan([{"coil_barcode": "A"}])
		api.confirm_load(p, "A", client_event_id="a")
		# re-trigger completion handler — must not create a second confirmation
		outbound_sap.on_plan_completed(frappe.get_doc("Loading Plan", p))
		self.assertEqual(frappe.db.count("WMSLite SAP Confirmation", {"loading_plan": p}), 1)

	def test_short_load_held_then_release(self):
		p = self._plan([{"coil_barcode": "A"}, {"coil_barcode": "B"}])
		api.confirm_load(p, "A", client_event_id="a")
		api.request_short_load(p, [{"coil_barcode": "B", "reason": "Damaged"}])
		api.approve_short_load(p, "1111")  # -> Completed (Short)
		conf = self._conf(p)
		self.assertEqual(conf.status, "Held")
		outbound_sap.release(conf.name)
		self.assertEqual(frappe.db.get_value("WMSLite SAP Confirmation", conf.name, "status"), "Pending")
		r = outbound_sap.send(conf.name)
		self.assertEqual(r["status"], "Confirmed")

	def test_business_error_parks(self):
		p = self._plan([{"coil_barcode": "A"}])
		api.confirm_load(p, "A", client_event_id="a")
		conf = self._conf(p)
		self._resp = FakeResp(400, {"error": "stock mismatch"})
		r = outbound_sap.send(conf.name)
		self.assertEqual(r["status"], "Failed (Business)")

	def test_transport_error_retries_then_exhausts(self):
		p = self._plan([{"coil_barcode": "A"}])
		api.confirm_load(p, "A", client_event_id="a")
		conf = self._conf(p)
		self._resp = FakeResp(500, {"err": "boom"})
		r1 = outbound_sap.send(conf.name)
		self.assertEqual(r1["status"], "Failed (Retryable)")
		# reset to sendable and send again -> attempts hits max (2) -> Exhausted
		frappe.db.set_value("WMSLite SAP Confirmation", conf.name, "status", "Failed (Retryable)")
		frappe.db.commit()
		r2 = outbound_sap.send(conf.name)
		self.assertEqual(r2["status"], "Exhausted")

	def test_reopen_blocked_after_gi_confirmed(self):
		p = self._plan([{"coil_barcode": "A"}])
		api.confirm_load(p, "A", client_event_id="a")
		outbound_sap.send(self._conf(p).name)  # Confirmed
		with self.assertRaises(frappe.ValidationError):
			console_api.cancel_plan(p, reopen=1)
