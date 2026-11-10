"""Unit tests for Inventory Count (bin<->coil mapping capture).

Covers count_scan (create txn + derive Bin Inventory, In-app source), idempotent
offline replay, move + already-here detection, count_open_bin resume (blind
count: only the operator's own scans today), QC toggle,
count_remove (revert to previous bin / drop a brand-new mis-scan / protect a
SAP-fed row), my_tasks gating, role enforcement, and the offline queue replay.

All data is __TEST__-prefixed and torn down per the prod-test convention. Run:

    bench --site <site> run-tests --app sbx_wmslite --module sbx_wmslite.tests.test_count
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from sbx_wmslite import api, bin_api, count_api
from sbx_wmslite.tests.settings_guard import SettingsGuard

BIN_A = "__TEST__BIN_A"
BIN_B = "__TEST__BIN_B"


# Every field these tests write is listed here so tearDownClass can put the
# site's own values back — this Single is shared with the live site.
SETTINGS = SettingsGuard(
	"count_enabled",
	"count_qc_default",
	"count_push_enabled",
	"qr_decode_rule",
	"bin_decode_rule",
	"bin_validation_enabled",
)


def _settings():
	SETTINGS.apply(
		count_enabled=1, count_qc_default="OK",
		# This suite does NOT mock SAP. With the count push ON, every count_scan
		# here would enqueue a real sweep and POST __TEST__ coils to SAP — which
		# happened on 2026-09-23 (SAP rejected them, but they were real calls).
		# Force it OFF for the run; scans park at "Disabled" and nothing is sent.
		count_push_enabled=0,
		qr_decode_rule="Identity", bin_decode_rule="Identity",
		# These cases scan __TEST__ bins that are deliberately not in the location
		# master, so validation is off here. It has its own coverage in
		# test_locations.py, including that count_open_bin honours it.
		bin_validation_enabled=0,
	)


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
		SETTINGS.restore()
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

	# ---- open bin (resume; blind count) ----
	def test_open_bin_lists_coils(self):
		count_api.count_scan(BIN_A, self._coil(1), client_event_id="o1")
		count_api.count_scan(BIN_A, self._coil(2), client_event_id="o2", qc=1)
		out = count_api.count_open_bin(BIN_A)
		self.assertEqual(out["bin_code"], BIN_A)
		self.assertEqual(out["count"], 2)
		self.assertEqual(out["qc_count"], 1)
		# each resumed row carries the scan's txn_key so ✕ can reverse it exactly
		self.assertEqual({c["coil_barcode"]: c["cid"] for c in out["coils"]},
						 {self._coil(1): "o1", self._coil(2): "o2"})

	def test_open_bin_hides_sap_mapped_coils(self):
		# Blind count: coils SAP maps to the bin are NOT shown until scanned.
		coil = self._coil(1)
		bin_api.upsert_bin(coil, {"bin_code": BIN_A, "material_grade": "X52",
								  "weight": 4200}, source="Push API")
		frappe.db.commit()
		out = count_api.count_open_bin(BIN_A)
		self.assertEqual(out["count"], 0)
		self.assertEqual(out["coils"], [])
		# once scanned it appears, enriched with SAP grade/weight
		count_api.count_scan(BIN_A, coil, client_event_id="sap-o1")
		out = count_api.count_open_bin(BIN_A)
		self.assertEqual(out["count"], 1)
		self.assertEqual(out["coils"][0]["material_grade"], "X52")
		self.assertEqual(out["coils"][0]["weight"], 4200)

	def test_open_bin_excludes_reversed_other_bin_and_other_users(self):
		keep, undone, elsewhere, theirs = (self._coil(n) for n in (1, 2, 3, 4))
		count_api.count_scan(BIN_A, keep, client_event_id="x-keep")
		count_api.count_scan(BIN_A, undone, client_event_id="x-undone")
		count_api.count_remove(BIN_A, undone, client_event_id="x-undone")
		count_api.count_scan(BIN_B, elsewhere, client_event_id="x-elsewhere")
		count_api.count_scan(BIN_A, theirs, client_event_id="x-theirs")
		# another operator's scan in the same bin must not appear in my resume list
		txn = frappe.db.get_value("Coil Transaction", {"txn_key": "x-theirs"}, "name")
		frappe.db.set_value("Coil Transaction", txn, "username", "__test__other@example.com",
							update_modified=False)
		frappe.db.commit()
		out = count_api.count_open_bin(BIN_A)
		self.assertEqual([c["coil_barcode"] for c in out["coils"]], [keep])

	def test_open_bin_dedupes_rescan(self):
		# Re-scanning a coil already in the bin records another txn but lists once.
		coil = self._coil(1)
		count_api.count_scan(BIN_A, coil, client_event_id="d1")
		count_api.count_scan(BIN_A, coil, client_event_id="d2")
		self.assertEqual(frappe.db.count("Coil Transaction", {"coil_barcode": coil}), 2)
		out = count_api.count_open_bin(BIN_A)
		self.assertEqual(out["count"], 1)
		self.assertEqual(out["coils"][0]["cid"], "d2")

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
