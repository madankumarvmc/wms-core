"""Tests for the console's inventory + scan-transaction views.

These endpoints are read-only reporting over two big shared tables, so the things
worth pinning are: the permission gate, that pagination reports a total matching
the FILTERS (not the table size), and that the source filter actually separates
the SAP feed from in-app Inventory Count scans — they share one doctype.

__TEST__-namespaced rows only; cleaned up in tearDownClass.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from sbx_wmslite import console_api

BIN_A = "__TEST__BIN-A"
BIN_B = "__TEST__BIN-B"
COILS = ["__TEST__C1", "__TEST__C2", "__TEST__C3"]
PLAIN_USER = "__test__console_plain@wms.local"


def _mkbin(coil, bin_code, weight, status="Available"):
	if frappe.db.exists("Bin Inventory", coil):
		frappe.delete_doc("Bin Inventory", coil, force=1, ignore_permissions=True)
	d = frappe.new_doc("Bin Inventory")
	d.coil_barcode = coil
	d.bin_code = bin_code
	d.weight = weight
	d.status = status
	d.source = "Upload"
	d.insert(ignore_permissions=True)
	return d.name


def _mktxn(coil, bin_code, source, txn_type, when):
	key = f"__TEST__{coil}-{source}-{when}"
	if frappe.db.exists("Coil Transaction", {"txn_key": key}):
		return
	d = frappe.new_doc("Coil Transaction")
	d.coil_barcode = coil
	d.bin_code = bin_code
	d.source = source
	d.transaction_type = txn_type
	d.scanned_at = when
	d.txn_key = key
	d.processed = 1
	d.raw_json = '{"BATCH": "%s", "LOCATION": "%s"}' % (coil, bin_code)
	d.insert(ignore_permissions=True)
	return d.name


def _cleanup():
	frappe.set_user("Administrator")
	for dt, field in (("Coil Transaction", "txn_key"), ("Bin Inventory", "coil_barcode")):
		for n in frappe.get_all(dt, filters={field: ["like", "%__TEST__%"]}, pluck="name"):
			frappe.delete_doc(dt, n, force=1, ignore_permissions=True)
	if frappe.db.exists("User", PLAIN_USER):
		frappe.delete_doc("User", PLAIN_USER, force=1, ignore_permissions=True)
	frappe.db.commit()


class TestConsoleInventoryViews(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_cleanup()
		_mkbin(COILS[0], BIN_A, 1000)
		_mkbin(COILS[1], BIN_A, 1500, status="Picked")
		_mkbin(COILS[2], BIN_B, 2000)
		# two populations in the same table, on purpose
		_mktxn(COILS[0], BIN_A, "Push API", None, "2026-08-01 10:00:00")
		_mktxn(COILS[1], BIN_A, "Push API", None, "2026-08-02 10:00:00")
		_mktxn(COILS[2], BIN_B, "In-app", "Inventory Count", "2026-08-03 10:00:00")
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		_cleanup()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	# ---- permission gate ----
	def test_endpoints_require_console_role(self):
		if not frappe.db.exists("User", PLAIN_USER):
			u = frappe.new_doc("User")
			u.email = PLAIN_USER
			u.first_name = "Plain"
			u.user_type = "System User"
			u.send_welcome_email = 0
			u.insert(ignore_permissions=True)
			frappe.db.commit()
		frappe.set_user(PLAIN_USER)
		for fn in (console_api.get_bin_rollup, console_api.get_coil_transactions,
				   console_api.reprocess_transactions, console_api.export_dashboard):
			with self.assertRaises(frappe.PermissionError):
				fn()
		with self.assertRaises(frappe.PermissionError):
			console_api.export_view(view="transactions")

	# ---- bin rollup ----
	def test_rollup_groups_by_bin(self):
		d = console_api.get_bin_rollup(q="__TEST__BIN")
		by_bin = {r["bin_code"]: r for r in d["rows"]}
		self.assertEqual(d["total"], 2)                       # two distinct bins
		self.assertEqual(by_bin[BIN_A]["coils"], 2)
		self.assertEqual(float(by_bin[BIN_A]["weight"]), 2500.0)
		self.assertEqual(float(by_bin[BIN_A]["picked"]), 1.0)
		self.assertEqual(by_bin[BIN_B]["coils"], 1)

	def test_rollup_paginates(self):
		first = console_api.get_bin_rollup(q="__TEST__BIN", limit=1, start=0)
		second = console_api.get_bin_rollup(q="__TEST__BIN", limit=1, start=1)
		self.assertEqual(len(first["rows"]), 1)
		self.assertEqual(len(second["rows"]), 1)
		# same filtered total on both pages, different rows
		self.assertEqual(first["total"], 2)
		self.assertEqual(second["total"], 2)
		self.assertNotEqual(first["rows"][0]["bin_code"], second["rows"][0]["bin_code"])

	# ---- coil list ----
	def test_coil_total_respects_filters(self):
		"""The pager says "of M" — M must be the filtered count, not 53k."""
		d = console_api.get_bin_inventory(bin_code=BIN_A)
		self.assertEqual(d["total"], 2)
		self.assertEqual({r["coil_barcode"] for r in d["rows"]}, {COILS[0], COILS[1]})
		q = console_api.get_bin_inventory(q="__TEST__C3")
		self.assertEqual(q["total"], 1)

	# ---- transactions ----
	def test_source_filter_separates_populations(self):
		sap = console_api.get_coil_transactions(q="__TEST__", source="Push API")
		count = console_api.get_coil_transactions(q="__TEST__", source="In-app")
		self.assertEqual(sap["total"], 2)
		self.assertEqual(count["total"], 1)
		self.assertEqual(count["rows"][0]["transaction_type"], "Inventory Count")

	def test_transactions_newest_first_and_date_filter(self):
		d = console_api.get_coil_transactions(q="__TEST__")
		self.assertEqual(d["total"], 3)
		stamps = [str(r["scanned_at"]) for r in d["rows"]]
		self.assertEqual(stamps, sorted(stamps, reverse=True))
		ranged = console_api.get_coil_transactions(q="__TEST__", date_from="2026-08-02", date_to="2026-08-03")
		self.assertEqual(ranged["total"], 2)

	def test_transaction_detail_returns_raw_payload(self):
		row = console_api.get_coil_transactions(q="__TEST__C3")["rows"][0]
		d = console_api.get_transaction_detail(row["name"])
		self.assertEqual(d["coil_barcode"], COILS[2])
		self.assertIn("BATCH", d["raw_json"])

	def test_transaction_detail_rejects_unknown(self):
		with self.assertRaises(frappe.ValidationError):
			console_api.get_transaction_detail("__TEST__does-not-exist")

	# ---- dashboard ----
	def test_dashboard_has_inventory_and_scan_sections(self):
		d = console_api.get_dashboard()
		self.assertIn("inventory", d)
		self.assertIn("scans", d)
		self.assertGreaterEqual(d["inventory"]["coils"], 3)
		self.assertGreaterEqual(d["inventory"]["bins"], 2)
		self.assertGreaterEqual(d["scans"]["total"], 3)
		# inventory is a live figure: a date range must not shrink it
		ranged = console_api.get_dashboard(date_from="2026-08-01", date_to="2026-08-01")
		self.assertEqual(ranged["inventory"]["coils"], d["inventory"]["coils"])
		self.assertLess(ranged["scans"]["total"], d["scans"]["total"])

	# ---- Excel export ----
	def _sheet(self, fn, **kw):
		"""Run an export and read the workbook back out of frappe.response."""
		import io

		import openpyxl

		frappe.response.clear()
		fn(**kw)
		self.assertEqual(frappe.response["type"], "binary")
		self.assertTrue(frappe.response["filename"].endswith(".xlsx"))
		ws = openpyxl.load_workbook(io.BytesIO(frappe.response["filecontent"])).active
		rows = [list(r) for r in ws.iter_rows(values_only=True)]
		# preamble, one blank spacer, header row, then the data
		blank = next(i for i, r in enumerate(rows) if not any(c not in (None, "") for c in r))
		return rows[:blank], rows[blank + 1], rows[blank + 2:]

	def test_export_rejects_unknown_view(self):
		with self.assertRaises(frappe.ValidationError):
			console_api.export_view(view="not-a-view")

	def test_export_carries_the_filtered_rows(self):
		"""The file must hold what the filter selected — not the page, not the table."""
		_pre, header, body = self._sheet(console_api.export_view, view="transactions",
										 q="__TEST__", source="In-app")
		self.assertEqual(header[1], "Coil")
		barcodes = [r[1] for r in body if r and r[1]]
		self.assertEqual(barcodes, [COILS[2]])

		_pre, _h, all_body = self._sheet(console_api.export_view, view="transactions", q="__TEST__")
		self.assertEqual(len([r for r in all_body if r and r[1]]), 3)

	def test_export_header_states_the_filters(self):
		"""A file that leaves the console without its filters gets misread."""
		pre, _h, _b = self._sheet(console_api.export_view, view="transactions",
								  q="__TEST__", source="In-app")
		note = next(r[0] for r in pre if r and str(r[0] or "").startswith("Filters:"))
		self.assertIn("search=__TEST__", note)
		self.assertIn("source=In-app", note)
		# a param the view never queries on must not be echoed as if it had applied
		pre_inv, _h, _b = self._sheet(console_api.export_view, view="inventory_bin",
									  q="__TEST__BIN", date_from="2026-08-01")
		note_inv = next(r[0] for r in pre_inv if r and str(r[0] or "").startswith("Filters:"))
		self.assertNotIn("from=", note_inv)

	def test_export_inventory_views(self):
		_p, header, body = self._sheet(console_api.export_view, view="inventory_bin", q="__TEST__BIN")
		self.assertEqual(header[0], "Bin")
		self.assertEqual({r[0] for r in body if r and r[0]}, {BIN_A, BIN_B})
		_p, _h, coils = self._sheet(console_api.export_view, view="inventory_coil", bin_code=BIN_A)
		self.assertEqual({r[1] for r in coils if r and r[1]}, {COILS[0], COILS[1]})

	def test_export_dashboard_is_metric_value_pairs(self):
		_p, header, body = self._sheet(console_api.export_dashboard)
		self.assertEqual(header, ["Section", "Metric", "Value"])
		metrics = {r[1] for r in body if r and r[1]}
		self.assertIn("Coils in stock", metrics)
		self.assertIn("Scans", metrics)
