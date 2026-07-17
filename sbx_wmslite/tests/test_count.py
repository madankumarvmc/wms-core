"""Unit tests for Inventory Count (bin<->coil mapping capture).

Covers count_scan (create txn + derive Bin Inventory, In-app source), idempotent
offline replay, move + already-here detection, count_open_bin resume, QC toggle,
count_remove (revert to previous bin / drop a brand-new mis-scan / protect a
SAP-fed row), my_tasks gating, role enforcement, and the offline queue replay.

All data is __TEST__-prefixed and torn down per the prod-test convention. Run:

    bench --site <site> run-tests --app sbx_wmslite --module sbx_wmslite.tests.test_count
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from sbx_wmslite import api, bin_api, count_api

BIN_A = "__TEST__BIN_A"
BIN_B = "__TEST__BIN_B"


def _settings():
	s = frappe.get_doc("WMSLite Settings")
	s.count_enabled = 1
	s.count_qc_default = "OK"
	s.qr_decode_rule = "Identity"
	s.bin_decode_rule = "Identity"
	s.save(ignore_permissions=True)
	frappe.db.commit()


def _cleanup():
	for dt, field in (("Coil Transaction", "coil_barcode"), ("Bin Inventory", "coil_barcode")):
		for n in frappe.get_all(dt, filters={field: ["like", "__TEST__%"]}, pluck="name"):
			frappe.delete_doc(dt, n, force=1, ignore_permissions=True)
	frappe.db.commit()


class TestInventoryCount(FrappeTestCase):
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
		frappe.set_user("Administrator")
		_cleanup()

	def _coil(self, n):
		return "__TEST__C_" + self._testMethodName + "_" + str(n)

	# ---- scan + derive ----
	def test_scan_creates_txn_and_derives(self):
		coil = self._coil(1)
		r = count_api.count_scan(BIN_A, coil, client_event_id="ut-c1", qc=0)
		self.assertEqual(r["coil_id"], coil)
		self.assertEqual(r["moved"], 0)
		self.assertEqual(r["existing"], 0)
		# transaction recorded, In-app, Inventory Count
		txn = frappe.get_all("Coil Transaction", filters={"txn_key": "ut-c1"},
							 fields=["source", "transaction_type", "bin_code", "processed"])
		self.assertEqual(len(txn), 1)
		self.assertEqual(txn[0].source, "In-app")
		self.assertEqual(txn[0].transaction_type, "Inventory Count")
		self.assertEqual(txn[0].processed, 1)
		# Bin Inventory derived
		self.assertEqual(frappe.db.get_value("Bin Inventory", coil, "bin_code"), BIN_A)
		self.assertEqual(frappe.db.get_value("Bin Inventory", coil, "source"), "In-app")

	def test_scan_idempotent(self):
		coil = self._coil(1)
		count_api.count_scan(BIN_A, coil, client_event_id="ut-dup")
		r2 = count_api.count_scan(BIN_A, coil, client_event_id="ut-dup")
		self.assertTrue(r2.get("idempotent"))
		self.assertEqual(frappe.db.count("Coil Transaction", {"txn_key": "ut-dup"}), 1)

	def test_move_and_already_here(self):
		coil = self._coil(1)
		count_api.count_scan(BIN_A, coil, client_event_id="m1")
		r = count_api.count_scan(BIN_B, coil, client_event_id="m2")
		self.assertEqual(r["moved"], 1)
		self.assertEqual(r["previous_bin"], BIN_A)
		self.assertEqual(frappe.db.get_value("Bin Inventory", coil, "bin_code"), BIN_B)
		# scan again in the same bin -> already here, no move
		r2 = count_api.count_scan(BIN_B, coil, client_event_id="m3")
		self.assertEqual(r2["existing"], 1)
		self.assertEqual(r2["moved"], 0)

	def test_scan_preserves_sap_grade_weight(self):
		coil = self._coil(1)
		# SAP feed already knows grade/weight
		bin_api.upsert_bin(coil, {"bin_code": BIN_A, "material_grade": "X52",
								  "weight": 4200}, source="Push API")
		frappe.db.commit()
		count_api.count_scan(BIN_B, coil, client_event_id="g1")
		# location moved, but grade/weight are NOT blanked
		self.assertEqual(frappe.db.get_value("Bin Inventory", coil, "bin_code"), BIN_B)
		self.assertEqual(frappe.db.get_value("Bin Inventory", coil, "material_grade"), "X52")
		self.assertEqual(frappe.db.get_value("Bin Inventory", coil, "weight"), 4200)

	# ---- open bin (resume) ----
	def test_open_bin_lists_coils(self):
		count_api.count_scan(BIN_A, self._coil(1), client_event_id="o1")
		count_api.count_scan(BIN_A, self._coil(2), client_event_id="o2", qc=1)
		out = count_api.count_open_bin(BIN_A)
		self.assertEqual(out["bin_code"], BIN_A)
		self.assertEqual(out["count"], 2)
		self.assertEqual(out["qc_count"], 1)

	# ---- QC ----
	def test_set_qc(self):
		coil = self._coil(1)
		count_api.count_scan(BIN_A, coil, client_event_id="q1")
		count_api.count_set_qc(BIN_A, coil, 1)
		self.assertEqual(frappe.db.get_value("Bin Inventory", coil, "qc"), 1)
		count_api.count_set_qc(BIN_A, coil, 0)
		self.assertEqual(frappe.db.get_value("Bin Inventory", coil, "qc"), 0)

	# ---- remove / undo ----
	def test_remove_reverts_move(self):
		coil = self._coil(1)
		count_api.count_scan(BIN_A, coil, client_event_id="r1")
		count_api.count_scan(BIN_B, coil, client_event_id="r2")
		self.assertEqual(frappe.db.get_value("Bin Inventory", coil, "bin_code"), BIN_B)
		# undo the move -> back to BIN_A (latest remaining txn)
		count_api.count_remove(BIN_B, coil, client_event_id="r2")
		self.assertEqual(frappe.db.get_value("Bin Inventory", coil, "bin_code"), BIN_A)

	def test_remove_brand_new_drops_row(self):
		coil = self._coil(1)
		count_api.count_scan(BIN_A, coil, client_event_id="rn1")
		count_api.count_remove(BIN_A, coil, client_event_id="rn1")
		# no prior location -> row removed
		self.assertFalse(frappe.db.exists("Bin Inventory", coil))

	def test_remove_protects_sap_row(self):
		coil = self._coil(1)
		bin_api.upsert_bin(coil, {"bin_code": BIN_A, "material_grade": "X"}, source="Push API")
		frappe.db.commit()
		# a count mis-scans it into BIN_B, then undoes -> must restore to BIN_A, not delete
		count_api.count_scan(BIN_B, coil, client_event_id="sp1")
		count_api.count_remove(BIN_B, coil, client_event_id="sp1")
		self.assertTrue(frappe.db.exists("Bin Inventory", coil))
		self.assertEqual(frappe.db.get_value("Bin Inventory", coil, "bin_code"), BIN_A)

	# ---- tasks / roles ----
	def test_my_tasks_reflects_flag(self):
		out = count_api.my_tasks()  # Administrator = System Manager
		self.assertIn("loading", out["tasks"])
		self.assertIn("count", out["tasks"])
		# flag off -> count drops out
		frappe.db.set_single_value("WMSLite Settings", "count_enabled", 0)
		self.assertNotIn("count", count_api.my_tasks()["tasks"])
		frappe.db.set_single_value("WMSLite Settings", "count_enabled", 1)

	def test_role_enforced(self):
		frappe.set_user("Guest")  # no count role
		try:
			with self.assertRaises(frappe.PermissionError):
				count_api.count_scan(BIN_A, self._coil(1), client_event_id="perm1")
		finally:
			frappe.set_user("Administrator")

	# ---- offline replay ----
	def test_offline_queue_replay(self):
		coil = self._coil(1)
		events = [
			{"type": "count_scan", "bin_code": BIN_A, "raw_qr": coil,
			 "coil_barcode": coil, "qc": 0, "client_event_id": "off1"},
			# a replay of the same event must not double-insert
			{"type": "count_scan", "bin_code": BIN_A, "raw_qr": coil,
			 "coil_barcode": coil, "qc": 0, "client_event_id": "off1"},
		]
		results = api.submit_offline_queue(json.dumps(events))
		self.assertTrue(all(r.get("ok") is not False for r in results))
		self.assertEqual(frappe.db.count("Coil Transaction", {"txn_key": "off1"}), 1)
		self.assertEqual(frappe.db.get_value("Bin Inventory", coil, "bin_code"), BIN_A)
