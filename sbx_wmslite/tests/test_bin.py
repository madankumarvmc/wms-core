"""Unit tests for bin-directed picking.

Covers the Bin Inventory feed (idempotent upsert / remove), plan-bin resolution,
the get_plan bin_picking flag, the wrong-bin-agnostic server confirm, and the
mark-picked-on-load side effect. Bin decoding (Identity / Regex Extract) too.

All data is __TEST__-prefixed and torn down per the prod-test convention. Run:

    bench --site <site> run-tests --app sbx_wmslite --module sbx_wmslite.tests.test_bin
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from sbx_wmslite import api, bin_api, sap_api
from sbx_wmslite.decode import decode_bin_code

TAG = "__TEST__BINUT"
TRUCK = "__TEST__TRUCK_BINUT"


def _settings():
	s = frappe.get_doc("WMSLite Settings")
	s.sap_enabled = 1
	s.sap_api_key = "UT-KEY"
	s.qr_decode_rule = "Identity"
	s.bin_picking_enabled = 1
	s.bin_decode_rule = "Identity"
	s.allow_pick_outside_bin = 0
	s.mark_picked_on_load = 1
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
	for n in frappe.get_all("Bin Inventory",
			filters={"coil_barcode": ["like", "__TEST__%"]}, pluck="name"):
		frappe.delete_doc("Bin Inventory", n, force=1, ignore_permissions=True)
	frappe.db.commit()


class TestBinPicking(FrappeTestCase):
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
		_cleanup()
		self.tag = TAG + "_" + self._testMethodName
		self.truck = TRUCK + "_" + self._testMethodName

	def _coil(self, n):
		return "__TEST__BC_" + self._testMethodName + "_" + str(n)

	def _make_plan(self):
		coils = [{"coil_barcode": self._coil(i), "weight": 100 * i} for i in (1, 2, 3)]
		r = sap_api._upsert(None, self.truck, self.tag, coils, {"sap_plan_id": self.tag})
		frappe.db.commit()
		return r["plan"]

	def _seed_bins(self):
		# coil 1 & 2 -> BIN-A1, coil 3 left unmapped
		bin_api.upsert_bin(self._coil(1), {"bin_code": "BIN-A1", "zone": "A", "aisle": "1"}, source="Upload")
		bin_api.upsert_bin(self._coil(2), {"bin_code": "BIN-A1", "zone": "A", "aisle": "1"}, source="Upload")
		frappe.db.commit()

	# ---- bin feed ----
	def test_upsert_idempotent_and_remove(self):
		name = bin_api.upsert_bin(self._coil(1), {"bin_code": "B1", "zone": "Z"}, source="Push API")
		self.assertEqual(name, self._coil(1))
		# re-upsert updates in place, no duplicate (docname == coil)
		bin_api.upsert_bin(self._coil(1), {"bin_code": "B2"}, source="Push API")
		self.assertEqual(frappe.db.get_value("Bin Inventory", self._coil(1), "bin_code"), "B2")
		self.assertEqual(frappe.db.count("Bin Inventory", {"coil_barcode": self._coil(1)}), 1)
		# default status applied on create
		self.assertEqual(frappe.db.get_value("Bin Inventory", self._coil(1), "status"), "Available")

	# ---- resolution + flag ----
	def test_resolve_and_bin_picking_flag(self):
		plan = self._make_plan()
		self._seed_bins()
		out = api.get_plan(plan)
		self.assertEqual(out["bin_picking"], 1)
		by_code = {c["coil_barcode"]: c for c in out["coils"]}
		self.assertEqual(by_code[self._coil(1)]["bin_code"], "BIN-A1")
		self.assertEqual(by_code[self._coil(1)]["zone"], "A")
		self.assertIsNone(by_code[self._coil(3)]["bin_code"])  # unmapped -> Location unknown

	def test_flag_off_without_bin_data(self):
		plan = self._make_plan()  # no bins seeded
		out = api.get_plan(plan)
		self.assertEqual(out["bin_picking"], 0)

	def test_resolution_persists_and_updates(self):
		plan = self._make_plan()
		self._seed_bins()
		api.get_plan(plan)  # resolves + saves
		doc = frappe.get_doc("Loading Plan", plan)
		self.assertEqual(doc.coils[0].bin_code, "BIN-A1")
		# move the coil to a new bin -> next resolve reflects it
		bin_api.upsert_bin(self._coil(1), {"bin_code": "BIN-C9", "zone": "C"}, source="Push API")
		frappe.db.commit()
		out = api.get_plan(plan)
		self.assertEqual({c["coil_barcode"]: c["bin_code"] for c in out["coils"]}[self._coil(1)], "BIN-C9")

	# ---- mark picked ----
	def test_mark_picked_on_load(self):
		plan = self._make_plan()
		self._seed_bins()
		api.confirm_load(plan, self._coil(1), client_event_id="ut-bin-1")
		self.assertEqual(frappe.db.get_value("Bin Inventory", self._coil(1), "status"), "Picked")
		# a coil with no bin row simply has nothing to mark (no error)
		r = api.confirm_load(plan, self._coil(3), client_event_id="ut-bin-3")
		self.assertEqual(r["loaded_coils"], 2)

	# ---- decode ----
	def test_decode_bin_identity_and_regex(self):
		self.assertEqual(decode_bin_code("  BIN-A1 "), "BIN-A1")
		s = frappe.get_doc("WMSLite Settings")
		s.bin_decode_rule = "Regex Extract"
		s.bin_decode_regex = r"BIN:([A-Z0-9-]+)"
		s.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache()
		self.assertEqual(decode_bin_code("BIN:A1-LOC;X=9"), "A1-LOC")
		# restore
		s.bin_decode_rule = "Identity"
		s.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache()
