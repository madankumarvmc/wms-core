"""Tests for the multi-shipment / By-Truck loading model.

Covers the SAP intake fix (a new shipment never hijacks another open shipment on
the same truck) and the truck_api bundle (aggregate, claim, scan-resolve,
whole-truck complete), plus operator_home By-Truck grouping.

__TEST__-namespaced; run:
    bench --site <site> run-tests --app sbx_wmslite --module sbx_wmslite.tests.test_truck
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from sbx_wmslite import api, sap_api, truck_api

TRUCK = "__TEST__TRK_MS"
DD = "2026-07-15"


def _settings():
	s = frappe.get_doc("WMSLite Settings")
	s.sap_enabled = 1
	s.sap_api_key = "UT-KEY"
	s.qr_decode_rule = "Identity"
	s.allow_plan_remove_on_resync = 1
	s.loading_push_enabled = 0
	s.gi_enabled = 0
	s.loading_group_mode = "By Shipment"
	s.completion_mode = "Whole Truck"
	s.save(ignore_permissions=True)
	frappe.db.commit()


def _cleanup():
	for dt, field in (("Coil Load Event", "truck_number"), ("Loading Plan", "truck_number")):
		for n in frappe.get_all(dt, filters={field: ["like", "__TEST__%"]}, pluck="name"):
			frappe.delete_doc(dt, n, force=1, ignore_permissions=True)
	frappe.db.commit()


class TestTruckModel(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_settings()
		_cleanup()

	@classmethod
	def tearDownClass(cls):
		_cleanup()
		frappe.db.set_single_value("WMSLite Settings", "loading_group_mode", "By Shipment")
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		_cleanup()

	def _ship(self, sap_id, codes, dd=DD):
		coils = [{"coil_barcode": c, "weight": 100, "material_grade": "GR"} for c in codes]
		existing = sap_api._locate_plan(sap_id, TRUCK)
		r = sap_api._upsert(existing, TRUCK, sap_id, coils, {"delivery_date": dd})
		frappe.db.commit()
		return r["plan"]

	# ---- intake: no hijack ----
	def test_locate_only_matches_sap_plan_id(self):
		a = self._ship("__TEST__SHIPA", ["__TEST__A1", "__TEST__A2"])
		# unknown shipment id => new (None), never the truck's other open plan
		self.assertIsNone(sap_api._locate_plan("__TEST__SHIPB", TRUCK))
		# known id => that plan
		self.assertEqual(sap_api._locate_plan("__TEST__SHIPA", TRUCK), a)
		# legacy (no id) => truck fallback
		self.assertEqual(sap_api._locate_plan("", TRUCK), a)

	def test_two_shipments_coexist_no_replace(self):
		a = self._ship("__TEST__SHIPA", ["__TEST__A1", "__TEST__A2"])
		b = self._ship("__TEST__SHIPB", ["__TEST__B1", "__TEST__B2"])
		self.assertNotEqual(a, b)
		da = frappe.get_doc("Loading Plan", a)
		db = frappe.get_doc("Loading Plan", b)
		# plan A untouched by the arrival of shipment B
		self.assertEqual(sorted(c.coil_barcode for c in da.coils), ["__TEST__A1", "__TEST__A2"])
		self.assertEqual(sorted(c.coil_barcode for c in db.coils), ["__TEST__B1", "__TEST__B2"])

	# ---- bundle ----
	def test_bundle_aggregate_and_scan(self):
		self._ship("__TEST__SHIPA", ["__TEST__A1", "__TEST__A2"])
		b = self._ship("__TEST__SHIPB", ["__TEST__B1"])
		bundle = truck_api.get_truck_bundle(TRUCK, DD)
		self.assertEqual(bundle["total_coils"], 3)
		self.assertEqual(len(bundle["shipments"]), 2)
		# scan resolves a coil to its owning shipment
		r = truck_api.scan_coil_bundle(TRUCK, DD, "__TEST__B1")
		self.assertTrue(r["matched"])
		self.assertEqual(r["plan"], b)

	def test_bundle_claim_all(self):
		self._ship("__TEST__SHIPA", ["__TEST__A1"])
		self._ship("__TEST__SHIPB", ["__TEST__B1"])
		res = truck_api.claim_truck_bundle(TRUCK, DD)
		self.assertTrue(res["ok"])
		for p in frappe.get_all("Loading Plan", filters={"truck_number": TRUCK}, pluck="name"):
			self.assertEqual(frappe.db.get_value("Loading Plan", p, "claimed_by"), "Administrator")

	def test_bundle_complete_refuses_then_completes(self):
		a = self._ship("__TEST__SHIPA", ["__TEST__A1", "__TEST__A2"])
		b = self._ship("__TEST__SHIPB", ["__TEST__B1"])
		# pending coils => refused with the list
		res = truck_api.complete_truck_bundle(TRUCK, DD)
		self.assertFalse(res["ok"])
		self.assertTrue(res["needs_short_load"])
		self.assertEqual(len(res["pending"]), 3)
		# load everything, then completing succeeds
		for plan, coil in [(a, "__TEST__A1"), (a, "__TEST__A2"), (b, "__TEST__B1")]:
			api.confirm_load(plan, coil, client_event_id="ut-" + coil)
		res2 = truck_api.complete_truck_bundle(TRUCK, DD)
		self.assertTrue(res2["ok"])
		for p in (a, b):
			self.assertIn(frappe.db.get_value("Loading Plan", p, "plan_status"),
						  ("Completed", "Completed (Short)"))

	# ---- home grouping ----
	def test_operator_home_by_truck_groups(self):
		self._ship("__TEST__SHIPA", ["__TEST__A1"])
		self._ship("__TEST__SHIPB", ["__TEST__B1"])
		frappe.db.set_single_value("WMSLite Settings", "loading_group_mode", "By Truck")
		frappe.clear_cache()
		home = api.operator_home()
		self.assertEqual(home["mode"], "By Truck")
		grp = [g for g in home["pending"] if g.get("truck_number") == TRUCK]
		self.assertEqual(len(grp), 1)
		self.assertEqual(grp[0]["shipment_count"], 2)
		self.assertEqual(grp[0]["total_coils"], 2)
		frappe.db.set_single_value("WMSLite Settings", "loading_group_mode", "By Shipment")
		frappe.clear_cache()
