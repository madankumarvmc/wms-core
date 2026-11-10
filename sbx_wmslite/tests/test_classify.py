"""Tests for sbx_wmslite.classify — the Site (bin-code prefix) and Material Type
(product keyword) rules behind the console's global filters.

What is pinned: the prefix rule and its SQL twin agree on the messy real-world
labels; the keyword rule is case-insensitive and editable; the master drives the
site list and its cache invalidates on save; the seed never overwrites an edited
site; and CoilTransaction.before_insert stores the same answer the backfill would.

__TEST__-namespaced rows only. Site saves are kept from enqueueing the background
backfill (frappe.flags.wms_skip_backfill); the backfill is exercised directly.

    bench --site <site> run-tests --app sbx_wmslite --module sbx_wmslite.tests.test_classify
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from sbx_wmslite import classify
from sbx_wmslite.tests.settings_guard import SettingsGuard

SITE = "__TEST__SITE"
PFX = "ZZTS"          # a prefix no real label uses
P = "__TEST__CL"      # row namespace

SETTINGS = SettingsGuard("material_pipe_keywords")


def _cleanup():
	frappe.set_user("Administrator")
	for n in frappe.get_all("WMSLite Site", filters={"name": ["like", "%__TEST__%"]}, pluck="name"):
		frappe.delete_doc("WMSLite Site", n, force=1, ignore_permissions=True)
	for dt, field in (("Coil Transaction", "txn_key"), ("Bin Inventory", "coil_barcode"),
					  ("WMSLite Bin", "bin_code")):
		for n in frappe.get_all(dt, filters={field: ["like", f"%{P}%"]}, pluck="name"):
			frappe.delete_doc(dt, n, force=1, ignore_permissions=True)
	frappe.db.commit()
	classify.clear_cache()


def _bin(code):
	d = frappe.new_doc("WMSLite Bin")
	d.bin_code = code
	d.source = "Manual"
	d.insert(ignore_permissions=True)
	return d.name


def _inv(coil, bin_code, product=None):
	d = frappe.new_doc("Bin Inventory")
	d.coil_barcode = coil
	d.bin_code = bin_code
	d.product = product
	d.source = "Upload"
	d.insert(ignore_permissions=True)
	return d.name


def _txn(coil, bin_code="", product=None, rm_destination=None, txn_type=None, source="In-app"):
	d = frappe.new_doc("Coil Transaction")
	d.coil_barcode = coil
	d.bin_code = bin_code
	d.product = product
	d.rm_destination = rm_destination
	d.transaction_type = txn_type
	d.source = source
	d.scanned_at = "2026-08-01 10:00:00"
	d.txn_key = f"{P}_{frappe.generate_hash(length=8)}"
	d.processed = 1
	d.insert(ignore_permissions=True)
	return d.name


class TestClassify(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.flags.wms_skip_backfill = True
		_cleanup()
		classify.seed_sites()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		_cleanup()
		SETTINGS.restore()
		frappe.flags.wms_skip_backfill = False
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		classify.clear_cache()

	# ---- the prefix rule ----
	def test_prefix_of(self):
		cases = {
			"JPR/CORR/KP1": "JPR", "CCL2 /G18/ 36": "CCL2", "CCL1 D28/x": "CCL1",
			"CCL1-D14": "CCL1", "Jpr/mill 5": "JPR", " cc2/G21/35": "CC2",
			"UNKNOWN": "UNKNOWN", "RP/NFS/1": "RP",
		}
		for code, want in cases.items():
			self.assertEqual(classify.prefix_of(code), want, code)
		self.assertIsNone(classify.prefix_of(""))
		self.assertIsNone(classify.prefix_of(None))
		self.assertIsNone(classify.prefix_of("__TEST__X"))   # no leading token

	def test_prefix_sql_matches_python(self):
		"""The query-time rule and the insert-time rule must never disagree."""
		codes = [f"JPR/{P}/A", f"CCL2 /{P}/ 36", f"CCL1 D28/{P}", f"CCL1-D14/{P}", f"Jpr/{P} 5",
				 f"RP/{P}/1", f"PKL3/{P}"]
		for c in codes:
			_bin(c)
		frappe.db.commit()
		rows = frappe.db.sql(f"""SELECT bin_code, {classify.prefix_sql("bin_code")} p
			FROM `tabWMSLite Bin` WHERE bin_code LIKE %(q)s""", {"q": f"%{P}%"})
		self.assertEqual(len(rows), len(codes))
		for code, p in rows:
			self.assertEqual(p, classify.prefix_of(code), code)

	# ---- material ----
	def test_material_for_product(self):
		self.assertEqual(classify.material_for_product("GI PIPE"), classify.MAT_PIPES)
		self.assertEqual(classify.material_for_product("round pipe"), classify.MAT_PIPES)
		self.assertEqual(classify.material_for_product("gi tube"), classify.MAT_PIPES)
		self.assertEqual(classify.material_for_product("H.R.COIL"), classify.MAT_SHEET)
		self.assertEqual(classify.material_for_product("GC Sheet"), classify.MAT_SHEET)
		self.assertEqual(classify.material_for_product(""), classify.UNASSIGNED)
		self.assertEqual(classify.material_for_product(None), classify.UNASSIGNED)

	def test_material_keywords_come_from_settings(self):
		SETTINGS.apply(material_pipe_keywords="PIPE, rohr")
		classify.clear_cache()
		self.assertEqual(classify.material_for_product("ROHR 40"), classify.MAT_PIPES)
		self.assertEqual(classify.material_for_product("GI TUBE"), classify.MAT_SHEET)   # TUBE no longer a keyword
		# the SQL twin reads the same keywords
		_inv(f"{P}_K1", f"JPR/{P}/K", "Rohr 40")
		frappe.db.commit()
		vals = {}
		where = classify.material_where("product", classify.MAT_PIPES, vals)
		got = frappe.db.sql_list(f"SELECT coil_barcode FROM `tabBin Inventory` WHERE coil_barcode LIKE %(q)s AND {where}",
								 dict(vals, q=f"{P}_K%"))
		self.assertEqual(got, [f"{P}_K1"])

	# ---- site master + cache ----
	def test_site_master_drives_site_and_invalidates_cache(self):
		self.assertEqual(classify.site_for_bin(f"{PFX}/1"), classify.UNASSIGNED)
		self.assertNotIn(SITE, classify.site_options())
		doc = frappe.get_doc({"doctype": "WMSLite Site", "site_name": SITE, "sort_order": 99,
							  "plant_codes": "9999", "prefixes": [{"bin_prefix": " zzts "}]})
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		# no manual clear_cache(): the controller's on_update did it
		self.assertEqual(doc.prefixes[0].bin_prefix, PFX)       # normalised on save
		self.assertEqual(classify.site_for_bin(f"{PFX}/1"), SITE)
		self.assertEqual(classify.site_for_plant("9999"), SITE)
		self.assertEqual(classify.site_options()[-1], classify.UNASSIGNED)
		self.assertIn(SITE, classify.site_options())
		frappe.delete_doc("WMSLite Site", SITE, force=1, ignore_permissions=True)
		frappe.db.commit()
		self.assertEqual(classify.site_for_bin(f"{PFX}/1"), classify.UNASSIGNED)

	def test_prefix_cannot_belong_to_two_sites(self):
		other = "ZZTA"   # its own prefix: this test must not leak into the cache test
		frappe.get_doc({"doctype": "WMSLite Site", "site_name": SITE + "_A",
						"prefixes": [{"bin_prefix": other}]}).insert(ignore_permissions=True)
		try:
			with self.assertRaises(frappe.ValidationError):
				frappe.get_doc({"doctype": "WMSLite Site", "site_name": SITE + "_B",
								"prefixes": [{"bin_prefix": other}]}).insert(ignore_permissions=True)
			with self.assertRaises(frappe.ValidationError):   # not a single token
				frappe.get_doc({"doctype": "WMSLite Site", "site_name": SITE + "_C",
								"prefixes": [{"bin_prefix": "AB/CD"}]}).insert(ignore_permissions=True)
		finally:
			frappe.delete_doc("WMSLite Site", SITE + "_A", force=1, ignore_permissions=True)
			frappe.db.commit()

	def test_seed_sites_is_idempotent_and_never_overwrites(self):
		self.assertEqual(classify.seed_sites()["created"], 0)
		site = frappe.get_doc("WMSLite Site", "Jangalpur")
		self.assertIn("JPR", [r.bin_prefix for r in site.prefixes])
		self.assertEqual(classify.site_for_bin("JPR/KMILL/H2/R1"), "Jangalpur")
		self.assertEqual(classify.site_for_bin("CCL3/A9"), "Ranihati")
		self.assertEqual(classify.site_for_bin("RP/NFS/1"), "Pipe Division")
		self.assertEqual(classify.site_for_bin("PKL3/x"), classify.UNASSIGNED)

	# ---- SQL predicates ----
	def test_site_where_and_unassigned_bucket(self):
		_inv(f"{P}_S1", f"JPR/{P}/A", "H.R.COIL")
		_inv(f"{P}_S2", f"CC2/{P}/B", "GC Sheet")
		_inv(f"{P}_S3", f"PKL3/{P}/D", None)
		frappe.db.commit()

		def coils(site):
			vals = {"q": f"{P}_S%"}
			where = classify.site_where("bin_code", site, vals)
			return set(frappe.db.sql_list(
				f"SELECT coil_barcode FROM `tabBin Inventory` WHERE coil_barcode LIKE %(q)s AND {where}", vals))
		self.assertEqual(coils("Jangalpur"), {f"{P}_S1"})
		self.assertEqual(coils("Ranihati"), {f"{P}_S2"})
		self.assertEqual(coils(classify.UNASSIGNED), {f"{P}_S3"})
		self.assertEqual(coils("No Such Site"), set())
		# site_sql labels every row the same way
		vals = {"q": f"{P}_S%"}
		rows = dict(frappe.db.sql(
			f"SELECT coil_barcode, {classify.site_sql('bin_code', vals)} FROM `tabBin Inventory` WHERE coil_barcode LIKE %(q)s", vals))
		self.assertEqual(rows, {f"{P}_S1": "Jangalpur", f"{P}_S2": "Ranihati", f"{P}_S3": classify.UNASSIGNED})

	# ---- insert-time classification + backfill ----
	def test_before_insert_stores_site_and_material(self):
		_inv(f"{P}_T1", f"RP/{P}/C", "GI PIPE")
		feed = _txn(f"{P}_T0", f"CC2/{P}/B", product="H.R.COIL", source="Push API")
		count = _txn(f"{P}_T1", f"RP/{P}/C", txn_type="Inventory Count")          # no product on the row
		rm = _txn(f"{P}_T1", "", rm_destination=f"JPR/{P}/LINE", txn_type="RM Receipt")
		frappe.db.commit()
		get = lambda n: frappe.db.get_value("Coil Transaction", n, ["site", "material_type"])
		self.assertEqual(get(feed), ("Ranihati", classify.MAT_SHEET))
		self.assertEqual(get(count), ("Pipe Division", classify.MAT_PIPES))      # product via Bin Inventory
		self.assertEqual(get(rm), ("Jangalpur", classify.MAT_PIPES))             # site via the line

		# the backfill reaches the same answer
		frappe.db.sql("UPDATE `tabCoil Transaction` SET site = NULL, material_type = NULL WHERE txn_key LIKE %(q)s",
					  {"q": f"{P}%"})
		frappe.db.commit()
		self.assertIsNone(get(count)[0])
		classify.backfill_transactions(only_null=True)
		self.assertEqual(get(feed), ("Ranihati", classify.MAT_SHEET))
		self.assertEqual(get(count), ("Pipe Division", classify.MAT_PIPES))
		self.assertEqual(get(rm), ("Jangalpur", classify.MAT_PIPES))
