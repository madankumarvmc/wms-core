"""Unit tests for the handheld Inventory lookup (inventory_api).

What is pinned: every handheld role gets the task (and only while the flag is
on); a scanned string resolves as a BIN before it is tried as a coil (dashed bin
labels must never be chopped by the coil rule); a bare barcode and the full
descriptive coil QR both find the one row; unknown text falls back to a prefix
search; pages are keyset (no overlap, no gap) and ordered bin then coil.

All data is __TEST__IV-prefixed and torn down; every WMSLite Settings field
touched is restored.

    bench --site <site> run-tests --app sbx_wmslite --module sbx_wmslite.tests.test_inventory_view
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from sbx_wmslite import api, bin_api, count_api, inventory_api
from sbx_wmslite.tests.settings_guard import SettingsGuard

P = "__TEST__IV"
BIN1 = f"{P}/B1"                 # 25 coils: spans two pages of 20
BIN2 = f"{P}/B2"                 # 3 coils
BIN_DASH = f"{P}/A1-A2/R1-6"     # a real-world label shape the coil rule would mangle
OPER = "__test__iv_loader@wms.local"
RECORDER = "__test__iv_recorder@wms.local"

SETTINGS = SettingsGuard(
	"inventory_view_enabled",
	"inventory_page_size",
	"qr_decode_rule",
	"bin_decode_rule",
	"bin_validation_enabled",
)


def _coil(i):
	return f"{P}_C{i:02d}"


def _full_qr(coil):
	# The descriptive QR shape from decode.py's docstring, with our batch code inside.
	return f"Jangalpur CR3 - H.R.COIL-{coil} - 2.500 mm  X 1230 - 26.420 MT- H025001230C2"


def _mkuser(email, roles):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=1, ignore_permissions=True)
	d = frappe.new_doc("User")
	d.email = email
	d.first_name = "IV"
	d.user_type = "System User"
	d.send_welcome_email = 0
	for r in roles:
		d.append("roles", {"role": r})
	d.insert(ignore_permissions=True)
	return d.name


def _cleanup():
	frappe.set_user("Administrator")
	for dt, field in (("Coil Transaction", "coil_barcode"), ("Bin Inventory", "coil_barcode"),
					  ("WMSLite Bin", "bin_code")):
		for n in frappe.get_all(dt, filters={field: ["like", f"%{P}%"]}, pluck="name"):
			frappe.delete_doc(dt, n, force=1, ignore_permissions=True)
	for u in (OPER, RECORDER):
		if frappe.db.exists("User", u):
			frappe.delete_doc("User", u, force=1, ignore_permissions=True)
	frappe.db.commit()


class TestInventoryView(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_cleanup()
		SETTINGS.apply(inventory_view_enabled=1, inventory_page_size=20,
					   # the live site's rules: coil QRs are decoded, bins taken as printed
					   qr_decode_rule="Inventory Barcode", bin_decode_rule="Identity",
					   bin_validation_enabled=1)
		for code in (BIN1, BIN2, BIN_DASH):
			b = frappe.new_doc("WMSLite Bin")
			b.bin_code = code
			b.source = "Manual"
			b.insert(ignore_permissions=True)
		for i in range(1, 26):
			bin_api.upsert_bin(_coil(i), {"bin_code": BIN1, "material_grade": "GR50", "weight": 1000 + i,
										 "product": "H.R.COIL"})
		for i in range(26, 29):
			bin_api.upsert_bin(_coil(i), {"bin_code": BIN2, "material_grade": "GR60", "weight": 2000})
		bin_api.upsert_bin(_coil(30), {"bin_code": BIN_DASH, "product": "GI PIPE"})
		frappe.db.set_value("Bin Inventory", _coil(27), {"status": "Picked", "qc": 1}, update_modified=False)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		_cleanup()
		SETTINGS.restore()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	# ---- task + roles ----
	def test_every_handheld_role_gets_the_task(self):
		_mkuser(OPER, ["Loading Operator"])
		_mkuser(RECORDER, ["Inventory Recorder"])
		frappe.db.commit()
		for u in (OPER, RECORDER):
			frappe.set_user(u)
			self.assertIn("inventory", count_api.my_tasks()["tasks"])
			r = inventory_api.inventory_lookup(q=BIN2)
			self.assertEqual(r["mode"], "bin")
			self.assertEqual(r["total"], 3)
		frappe.set_user("Administrator")
		self.assertIn("inventory", count_api.my_tasks()["tasks"])

	def test_flag_off_hides_task_and_blocks_terminally(self):
		frappe.db.set_single_value("WMSLite Settings", "inventory_view_enabled", 0)
		try:
			self.assertNotIn("inventory", count_api.my_tasks()["tasks"])
			with self.assertRaises(frappe.ValidationError) as cm:
				inventory_api.inventory_lookup()
			self.assertTrue(api._terminal_failure(cm.exception))
		finally:
			frappe.db.set_single_value("WMSLite Settings", "inventory_view_enabled", 1)

	def test_guest_is_refused(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				inventory_api.inventory_lookup()
		finally:
			frappe.set_user("Administrator")

	# ---- resolution ----
	def test_bin_resolves_before_coil_even_with_dashes(self):
		# the coil rule would turn this label into "6"; it must be read as a bin
		r = inventory_api.inventory_lookup(q=BIN_DASH)
		self.assertEqual((r["mode"], r["value"], r["total"]), ("bin", BIN_DASH, 1))
		self.assertEqual(r["rows"][0]["coil_barcode"], _coil(30))
		self.assertEqual(inventory_api.resolve_query(BIN1), {"mode": "bin", "value": BIN1})
		self.assertEqual(inventory_api.resolve_query("  " + BIN2 + " "), {"mode": "bin", "value": BIN2})

	def test_bin_resolves_without_the_master_when_validation_is_off(self):
		SETTINGS.apply(bin_validation_enabled=0)
		try:
			frappe.delete_doc("WMSLite Bin", BIN2, force=1, ignore_permissions=True)
			frappe.db.commit()
			self.assertEqual(inventory_api.resolve_query(BIN2)["mode"], "bin")   # via Bin Inventory
		finally:
			b = frappe.new_doc("WMSLite Bin")
			b.bin_code = BIN2
			b.source = "Manual"
			b.insert(ignore_permissions=True)
			frappe.db.commit()
			SETTINGS.apply(bin_validation_enabled=1)

	def test_coil_resolves_from_bare_code_and_full_qr(self):
		for raw in (_coil(27), _full_qr(_coil(27))):
			r = inventory_api.inventory_lookup(q=raw)
			self.assertEqual((r["mode"], r["value"], r["total"]), ("coil", _coil(27), 1), raw)
			row = r["rows"][0]
			self.assertEqual(row["bin_code"], BIN2)
			self.assertEqual((row["status"], row["qc"], row["material_grade"]), ("Picked", 1, "GR60"))
			self.assertFalse(r["has_more"])
			self.assertIsNone(r["next"])

	def test_unknown_text_falls_back_to_prefix_search(self):
		r = inventory_api.inventory_lookup(q=f"{P}/B")           # two bins start with this
		self.assertEqual(r["mode"], "prefix")
		self.assertEqual(r["total"], 28)
		r = inventory_api.inventory_lookup(q=f"{P}_C2")           # coils C20..C28
		self.assertEqual((r["mode"], r["total"]), ("prefix", 9))
		self.assertEqual({x["coil_barcode"] for x in r["rows"]}, {_coil(i) for i in range(20, 29)})
		r = inventory_api.inventory_lookup(q=f"{P}_NOPE")
		self.assertEqual((r["mode"], r["value"], r["rows"], r["total"]), ("prefix", f"{P}_NOPE", [], 0))

	# ---- listing + paging ----
	def test_all_mode_is_ordered_by_bin_then_coil_and_pages_without_overlap(self):
		# scope to our rows through a prefix so other data on the site cannot interfere
		first = inventory_api.inventory_lookup(q=P)
		self.assertEqual(first["mode"], "prefix")
		self.assertEqual(first["total"], 29)
		self.assertEqual(len(first["rows"]), 20)
		self.assertTrue(first["has_more"])
		self.assertEqual(first["next"], {"after_bin": first["rows"][-1]["bin_code"],
										 "after_coil": first["rows"][-1]["coil_barcode"]})
		keys = [(x["bin_code"], x["coil_barcode"]) for x in first["rows"]]
		self.assertEqual(keys, sorted(keys))
		self.assertEqual(keys[0][0], BIN_DASH)                  # "/A1-..." sorts before "/B1"
		second = inventory_api.inventory_lookup(q=P, **first["next"])
		self.assertEqual(len(second["rows"]), 9)
		self.assertFalse(second["has_more"])
		self.assertIsNone(second["next"])
		seen = keys + [(x["bin_code"], x["coil_barcode"]) for x in second["rows"]]
		self.assertEqual(len(seen), len(set(seen)))             # no overlap
		self.assertEqual(len(seen), 29)                         # no gap
		self.assertEqual(seen, sorted(seen))
		# the unfiltered list (mode all) is served the same way
		everything = inventory_api.inventory_lookup()
		self.assertEqual(everything["mode"], "all")
		self.assertGreaterEqual(everything["total"], 29)
		self.assertEqual(len(everything["rows"]), 20)

	def test_bin_mode_pages_through_a_big_bin(self):
		r = inventory_api.inventory_lookup(q=BIN1)
		self.assertEqual((r["mode"], r["total"], len(r["rows"]), r["has_more"]), ("bin", 25, 20, True))
		r2 = inventory_api.inventory_lookup(q=BIN1, **r["next"])
		self.assertEqual((len(r2["rows"]), r2["has_more"]), (5, False))
		got = [x["coil_barcode"] for x in r["rows"] + r2["rows"]]
		self.assertEqual(got, [_coil(i) for i in range(1, 26)])

	def test_page_size_from_settings_and_cap(self):
		SETTINGS.apply(inventory_page_size=5)
		try:
			self.assertEqual(len(inventory_api.inventory_lookup(q=BIN1)["rows"]), 5)
			self.assertEqual(len(inventory_api.inventory_lookup(q=BIN1, limit=7)["rows"]), 7)
			self.assertEqual(len(inventory_api.inventory_lookup(q=P, limit=500)["rows"]), 29)  # capped at 100
		finally:
			SETTINGS.apply(inventory_page_size=20)
