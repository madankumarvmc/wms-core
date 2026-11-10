"""A scanned location has to be a real location.

Before `locations.resolve_location`, every flow that asked for a location took
whatever string came back from the decoder. That is how a coil QR became an RM
destination, and how a bin code in production ended up being a coil string with
two real coils parked in it. A mistyped bin silently opened an empty count, which
looks exactly like a bin that is genuinely empty.

Validation is behind `bin_validation_enabled` so a missing label can be worked
around without a deploy — these tests cover both positions of that switch.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from sbx_wmslite import count_api, locations, rm_api
from sbx_wmslite.tests.settings_guard import SettingsGuard

LINE = "__TEST__LOC/LINE/1"
BIN = "__TEST__LOC/BIN/1"
UNKNOWN = "__TEST__LOC/NOT/A/PLACE"
# A real descriptive coil QR — the thing that must never become a location.
COIL_QR = "Jangalpur CR3 - H.R.COIL-JJG40788 - 2.500 mm  X 1230 - 26.420 MT- H025001230C2"


def _wipe():
	for n in frappe.get_all("WMSLite Bin", filters={"bin_code": ["like", "__TEST__LOC%"]},
							pluck="name"):
		frappe.delete_doc("WMSLite Bin", n, force=1, ignore_permissions=True)
	frappe.db.commit()


class TestLocationValidation(FrappeTestCase):
	settings = SettingsGuard("rm_enabled", "count_enabled", "bin_decode_rule",
							 "qr_decode_rule", "bin_validation_enabled",
							 "count_push_enabled", "rm_push_enabled")

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# Both SAP pushes OFF: these cases call count_scan / rm_receive, which arm a
		# push, and SAP is not mocked here.
		cls.settings.apply(rm_enabled=1, count_enabled=1, bin_decode_rule="Identity",
						   qr_decode_rule="Identity", bin_validation_enabled=1,
						   count_push_enabled=0, rm_push_enabled=0)
		_wipe()
		for code in (LINE, BIN):
			doc = frappe.new_doc("WMSLite Bin")
			doc.bin_code = code
			doc.source = "Master"
			doc.insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		_wipe()
		cls.settings.restore()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")

	# ---- the guard itself ----
	def test_known_location_resolves(self):
		self.assertEqual(locations.resolve_location(LINE, "line"), LINE)

	def test_unknown_location_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			locations.resolve_location(UNKNOWN, "line")
		self.assertIn("not a known location", str(cm.exception))

	def test_a_coil_qr_is_refused_as_a_location(self):
		"""The exact mistake seen on the floor: a coil scanned into the line box."""
		with self.assertRaises(frappe.ValidationError) as cm:
			locations.resolve_location(COIL_QR, "destination")
		self.assertIn("not a known location", str(cm.exception))

	def test_refusal_is_terminal_for_the_offline_queue(self):
		from sbx_wmslite import api

		with self.assertRaises(frappe.ValidationError) as cm:
			locations.resolve_location(UNKNOWN, "line")
		self.assertTrue(api._terminal_failure(cm.exception))

	def test_disabled_location_is_refused(self):
		frappe.db.set_value("WMSLite Bin", BIN, "disabled", 1)
		frappe.db.commit()
		try:
			with self.assertRaises(frappe.ValidationError):
				locations.resolve_location(BIN, "bin")
		finally:
			frappe.db.set_value("WMSLite Bin", BIN, "disabled", 0)
			frappe.db.commit()

	def test_validation_can_be_switched_off(self):
		"""The escape hatch: a master missing a label must not stop the floor."""
		self.settings.apply(bin_validation_enabled=0)
		try:
			self.assertEqual(locations.resolve_location(UNKNOWN, "line"), UNKNOWN)
		finally:
			self.settings.apply(bin_validation_enabled=1)

	# ---- every flow that scans a location ----
	def test_rm_gate_refuses_an_unknown_destination(self):
		with self.assertRaises(frappe.ValidationError):
			rm_api.rm_open_destination(COIL_QR)

	def test_rm_line_refuses_an_unknown_line(self):
		with self.assertRaises(frappe.ValidationError):
			rm_api.rm_open_line(UNKNOWN)

	def test_inventory_count_refuses_an_unknown_bin(self):
		with self.assertRaises(frappe.ValidationError):
			count_api.count_open_bin(UNKNOWN)

	def test_known_locations_still_open(self):
		self.assertEqual(rm_api.rm_open_destination(LINE)["destination"], LINE)
		self.assertEqual(rm_api.rm_open_line(LINE)["destination"], LINE)
		self.assertEqual(count_api.count_open_bin(BIN)["bin_code"], BIN)
