"""Uploading the location master is an upsert, and must stay one.

The master is `WMSLite Bin`, whose docname IS the bin code, and live
`Bin Inventory` / `Coil Transaction` rows point at those names. So the one
property this upload can never acquire is the ability to remove a code: a
supervisor uploading last month's sheet would otherwise silently un-scan half
the plant, and with `bin_validation_enabled` on the floor finds out by being
refused at the bay.

The other standing hazard is the supplied exports themselves. They routinely
have descriptive coil QRs pasted into the location column — that is how a
78-character coil string once became a "location" holding two real coils. The
importer re-applies the `" - "` rule rather than trusting the file.

Codes go in verbatim. The inconsistent ones (`CC1 F17/3`, `CCL3/a6/a`) are what
is physically printed, so normalising them on the way in would stop them
scanning.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from sbx_wmslite import console_api

PREFIX = "__TEST__BM/"
EXISTING = PREFIX + "EXISTING/1"
NEW = PREFIX + "NEW/1"
MESSY = PREFIX + "ODD 2"          # a space where a slash "should" be
COIL_QR = "Jangalpur CR3 - H.R.COIL-JJG40788 - 2.500 mm  X 1230 - 26.420 MT- H025001230C2"


def _wipe():
	for n in frappe.get_all("WMSLite Bin", filters={"bin_code": ["like", PREFIX + "%"]},
							pluck="name"):
		frappe.delete_doc("WMSLite Bin", n, force=1, ignore_permissions=True)
	frappe.db.commit()


class TestBinMasterImport(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_wipe()

	@classmethod
	def tearDownClass(cls):
		_wipe()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe()
		doc = frappe.new_doc("WMSLite Bin")
		doc.bin_code = EXISTING
		doc.zone = "OLDZONE"
		doc.aisle = "OLDAISLE"
		doc.source = "Master"
		doc.insert(ignore_permissions=True)
		frappe.db.commit()

	# ---- the upsert ----
	def test_a_new_code_is_created_and_marked_as_an_upload(self):
		console_api._run_binmaster_import([{"bin_code": NEW, "zone": "Z1"}])
		self.assertTrue(frappe.db.exists("WMSLite Bin", NEW))
		self.assertEqual(frappe.db.get_value("WMSLite Bin", NEW, "source"), "Upload")
		self.assertEqual(frappe.db.get_value("WMSLite Bin", NEW, "zone"), "Z1")

	def test_an_existing_code_is_updated_not_duplicated(self):
		console_api._run_binmaster_import([{"bin_code": EXISTING, "zone": "NEWZONE"}])
		self.assertEqual(frappe.db.count("WMSLite Bin", {"bin_code": EXISTING}), 1)
		self.assertEqual(frappe.db.get_value("WMSLite Bin", EXISTING, "zone"), "NEWZONE")

	def test_a_blank_column_leaves_the_existing_value_alone(self):
		"""A partial sheet (bin_code + zone) must not wipe aisle and the flags."""
		console_api._run_binmaster_import([{"bin_code": EXISTING, "zone": "NEWZONE", "aisle": ""}])
		self.assertEqual(frappe.db.get_value("WMSLite Bin", EXISTING, "aisle"), "OLDAISLE")

	def test_a_code_absent_from_the_file_survives_untouched(self):
		"""The property the whole design turns on: upload is additive."""
		console_api._run_binmaster_import([{"bin_code": NEW}])
		self.assertTrue(frappe.db.exists("WMSLite Bin", EXISTING))
		self.assertEqual(frappe.db.get_value("WMSLite Bin", EXISTING, "disabled"), 0)

	def test_flags_round_trip(self):
		console_api._run_binmaster_import([{"bin_code": NEW, "is_line": "1", "disabled": "1"}])
		self.assertEqual(frappe.db.get_value("WMSLite Bin", NEW, "is_line"), 1)
		self.assertEqual(frappe.db.get_value("WMSLite Bin", NEW, "disabled"), 1)

	def test_codes_are_stored_verbatim(self):
		console_api._run_binmaster_import([{"bin_code": MESSY}])
		self.assertTrue(frappe.db.exists("WMSLite Bin", MESSY))

	# ---- what it refuses ----
	def test_a_coil_string_is_rejected_and_reported(self):
		inserted, errors, errlog = console_api._run_binmaster_import(
			[{"bin_code": COIL_QR}, {"bin_code": NEW}])
		self.assertEqual(inserted, 1)
		self.assertEqual(errors, 1)
		self.assertFalse(frappe.db.exists("WMSLite Bin", COIL_QR))
		self.assertTrue(any("coil label" in line for line in errlog))

	def test_a_blank_code_is_rejected(self):
		_inserted, errors, _errlog = console_api._run_binmaster_import([{"bin_code": "  "}])
		self.assertEqual(errors, 1)

	def test_a_code_repeated_in_one_file_is_reported_once(self):
		inserted, errors, _errlog = console_api._run_binmaster_import(
			[{"bin_code": NEW}, {"bin_code": NEW}])
		self.assertEqual(inserted, 1)
		self.assertEqual(errors, 1)

	# ---- the preview agrees with the commit ----
	def test_preview_split_matches_what_the_commit_does(self):
		rows = [{"bin_code": EXISTING, "zone": "Z"}, {"bin_code": NEW}, {"bin_code": COIL_QR}]
		new, existing, rejects = console_api._binmaster_split(rows)
		self.assertEqual([c for c, _r in new], [NEW])
		self.assertEqual([c for c, _r in existing], [EXISTING])
		self.assertEqual(len(rejects), 1)

		inserted, errors, _errlog = console_api._run_binmaster_import(rows)
		self.assertEqual(inserted, len(new) + len(existing))
		self.assertEqual(errors, len(rejects))


class TestExportReupload(FrappeTestCase):
	"""A sheet downloaded from the console must upload straight back.

	`_xlsx_response` writes caption lines above the header row, so row 0 of an
	exported file is a title, not the header. Without `_skip_preamble` the
	download-edit-reupload loop this feature is built around maps every column to
	a title string and imports nothing, reporting success.
	"""

	def test_preamble_is_skipped_to_find_the_header(self):
		rows = [["SBX WMSLite — Bin Master"], ["Exported 2026-09-22 by x"],
				["Filters: none (all rows)"], [],
				["Bin Code", "Zone", "Aisle", "Is Line", "Disabled", "Source"],
				["CC1/F1/A", "CC1", "F1", "0", "0", "Master"]]
		out = console_api._skip_preamble(rows, ["bin_code", "zone", "aisle", "is_line", "disabled"])
		self.assertEqual(out[0][0], "Bin Code")
		self.assertEqual(len(out), 2)

	def test_a_file_that_already_starts_with_its_header_is_untouched(self):
		rows = [["bin_code", "zone"], ["CC1/F1/A", "CC1"]]
		self.assertEqual(console_api._skip_preamble(rows, ["bin_code", "zone"]), rows)

	def test_no_columns_means_no_change(self):
		"""Guards the Loading Plan / Bin Inventory paths against this change."""
		rows = [["a", "b"], ["1", "2"]]
		self.assertEqual(console_api._skip_preamble(rows, None), rows)

	def test_a_file_with_no_recognisable_header_is_left_alone(self):
		rows = [["nothing", "matches"], ["1", "2"]]
		self.assertEqual(console_api._skip_preamble(rows, ["bin_code"]), rows)
