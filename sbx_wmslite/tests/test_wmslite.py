"""Unit/integration tests for SBX WMSLite.

Covers the SAP intake (idempotency, authoritative re-sync, cancel, terminal
protection) and the operator flow (claim, scan, confirm idempotency, reject,
over-scan, undo, short-load + PIN approval).

All data is created under the __TEST__ prefix and torn down per the prod-test
convention used on this bench. Run:

    bench --site <site> run-tests --app sbx_wmslite
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from sbx_wmslite import api, sap_api

TAG = "__TEST__WMS"
TRUCK = "__TEST__TRUCK_UT"


def _settings():
	s = frappe.get_doc("WMSLite Settings")
	s.sap_enabled = 1
	s.sap_api_key = "UT-KEY"
	s.qr_decode_rule = "Identity"
	s.allow_overscan = 1
	s.allow_plan_remove_on_resync = 1
	s.supervisor_pin = "9999"
	s.undo_window_seconds = 600
	s.save(ignore_permissions=True)
	frappe.db.commit()


def _cleanup():
	for n in frappe.get_all("Loading Plan",
			filters={"truck_number": ["like", "__TEST__%"]}, pluck="name"):
		frappe.delete_doc("Loading Plan", n, force=1, ignore_permissions=True)
	for n in frappe.get_all("Coil Load Event",
			filters={"truck_number": ["like", "__TEST__%"]}, pluck="name"):
		frappe.delete_doc("Coil Load Event", n, force=1, ignore_permissions=True)
	for n in frappe.get_all("WMSLite SAP Confirmation",
			filters={"truck_number": ["like", "__TEST__%"]}, pluck="name"):
		frappe.delete_doc("WMSLite SAP Confirmation", n, force=1, ignore_permissions=True)
	frappe.db.commit()


class TestWMSLite(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_settings()
		_cleanup()

	@classmethod
	def tearDownClass(cls):
		_cleanup()
		super().tearDownClass()

	def setUp(self):
		# The API commits, so FrappeTestCase's per-test rollback doesn't isolate
		# us — clean namespaced rows before each test and use a per-test plan id.
		_cleanup()
		self.tag = TAG + "_" + self._testMethodName
		self.truck = TRUCK + "_" + self._testMethodName

	def _make_plan(self, coils=None):
		coils = coils or [
			{"coil_barcode": "UT-A", "weight": 100},
			{"coil_barcode": "UT-B", "weight": 200},
			{"coil_barcode": "UT-C", "weight": 300},
		]
		r = sap_api._upsert(None, self.truck, self.tag, coils, {"sap_plan_id": self.tag})
		frappe.db.commit()
		return r["plan"]

	# ---- SAP intake ----
	def test_sap_create_and_idempotent_resend(self):
		plan = self._make_plan()
		self.assertEqual(frappe.db.get_value("Loading Plan", plan, "total_coils"), 3)
		# resend same id → no duplicate plan
		existing = frappe.db.get_value("Loading Plan", {"sap_plan_id": self.tag}, "name")
		r2 = sap_api._upsert(existing, self.truck, self.tag, [{"coil_barcode": "UT-A"}], {})
		self.assertEqual(r2["plan"], plan)

	def test_sap_authoritative_resync_removes_pending(self):
		plan = self._make_plan()
		r = sap_api._upsert(plan, self.truck, self.tag,
			[{"coil_barcode": "UT-A"}, {"coil_barcode": "UT-D"}], {})
		self.assertIn("UT-D", r["added"])
		self.assertIn("UT-B", r["removed"])
		self.assertIn("UT-C", r["removed"])

	def test_sap_resync_protects_loaded(self):
		plan = self._make_plan()
		api.confirm_load(plan, "UT-B", client_event_id="ut-load-b")
		# resend WITHOUT UT-B — it is Loaded, so must be protected
		r = sap_api._upsert(plan, self.truck, self.tag, [{"coil_barcode": "UT-A"}], {})
		self.assertIn("UT-B", r["protected"])
		doc = frappe.get_doc("Loading Plan", plan)
		self.assertTrue(any(c.coil_barcode == "UT-B" and c.coil_status == "Loaded" for c in doc.coils))

	def test_sap_cancel_and_terminal_protection(self):
		plan = self._make_plan()
		c = sap_api._cancel(plan, self.truck, self.tag, {})
		self.assertEqual(c["status"], "Cancelled")
		# resend to a cancelled plan is rejected
		r = sap_api._upsert(plan, self.truck, self.tag, [{"coil_barcode": "UT-A"}], {})
		self.assertFalse(r["ok"])

	# ---- operator flow ----
	def test_scan_confirm_and_idempotency(self):
		plan = self._make_plan()
		sc = api.scan_coil(plan, "UT-A")
		self.assertTrue(sc["matched"])
		r = api.confirm_load(plan, "UT-A", client_event_id="ut-1")
		self.assertEqual(r["loaded_coils"], 1)
		again = api.confirm_load(plan, "UT-A", client_event_id="ut-1")
		self.assertTrue(again.get("idempotent"))
		self.assertEqual(again["loaded_coils"], 1)

	def test_reject_paths(self):
		plan = self._make_plan()
		api.confirm_load(plan, "UT-A", client_event_id="ut-2")
		self.assertEqual(api.scan_coil(plan, "UT-A")["reason"], "already_loaded")
		self.assertEqual(api.scan_coil(plan, "NOPE")["reason"], "not_on_plan")

	def test_overscan_and_undo(self):
		plan = self._make_plan()
		ov = api.overscan_add(plan, "UT-EXTRA", client_event_id="ut-ov")
		self.assertTrue(ov["unplanned"])
		un = api.undo_last_load(plan, "UT-EXTRA", client_event_id="ut-un")
		# unplanned coil removed on undo
		doc = frappe.get_doc("Loading Plan", plan)
		self.assertFalse(any(c.coil_barcode == "UT-EXTRA" for c in doc.coils))

	def test_short_load_and_pin_approval(self):
		plan = self._make_plan()
		api.confirm_load(plan, "UT-A", client_event_id="ut-a")
		api.confirm_load(plan, "UT-B", client_event_id="ut-b")
		api.request_short_load(plan, [{"coil_barcode": "UT-C", "reason": "Damaged"}])
		self.assertEqual(frappe.db.get_value("Loading Plan", plan, "plan_status"), "Pending Approval")
		with self.assertRaises(frappe.ValidationError):
			api.approve_short_load(plan, "0000")
		r = api.approve_short_load(plan, "9999", remarks="ok")
		self.assertEqual(r["plan_status"], "Completed (Short)")

	def test_claim_conflict_and_takeover(self):
		plan = self._make_plan()
		api.claim_plan(plan)  # Administrator claims
		# simulate another user holding it
		frappe.db.set_value("Loading Plan", plan, "claimed_by", "someone@else.com")
		conflict = api.claim_plan(plan)
		self.assertTrue(conflict.get("conflict"))
		forced = api.claim_plan(plan, force=1)
		self.assertTrue(forced["ok"])
		self.assertTrue(forced["took_over"])
