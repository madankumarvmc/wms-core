"""Tests for the console's inventory + scan-transaction views.

These endpoints are read-only reporting over two big shared tables, so the things
worth pinning are: the permission gate, that pagination reports a total matching
the FILTERS (not the table size), and that the source filter actually separates
the SAP feed from in-app Inventory Count scans — they share one doctype.

__TEST__-namespaced rows only; cleaned up in tearDownClass.
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

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


class TestConsoleRmViews(FrappeTestCase):
	"""The RM console views — the only window a supervisor has onto the floor.

	RM Movement has no header document (a "trip" is just coil state), so if these
	views miss a row there is nowhere else to notice it. What is pinned here: the
	per-line rollup counts what is actually out, ageing is measured from the
	dispatch, flagged rows are findable, and clearing a flag is a console-only act.
	"""

	LINE = "__TEST__RMV_LINE"
	OTHER = "__TEST__RMV_OTHER"
	YARD = "__TEST__RMV_YARD"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._wipe()
		# c1: in transit and old. c2: in transit, fresh. c3: received + flagged.
		cls.c1, cls.c2, cls.c3 = "__TEST__RMV_C1", "__TEST__RMV_C2", "__TEST__RMV_C3"
		for c in (cls.c1, cls.c2, cls.c3):
			_mkbin(c, cls.YARD, 3000)
		old = add_to_date(now_datetime(), hours=-9)
		frappe.db.set_value("Bin Inventory", cls.c1, {
			"rm_status": "In Transit to Line", "rm_destination": cls.LINE,
			"rm_source_bin": cls.YARD, "rm_cycle": 1, "rm_dispatched_at": old,
			"rm_dispatched_by": "Administrator"}, update_modified=False)
		frappe.db.set_value("Bin Inventory", cls.c2, {
			"rm_status": "In Transit to Line", "rm_destination": cls.LINE,
			"rm_source_bin": cls.YARD, "rm_cycle": 1,
			"rm_dispatched_at": now_datetime(), "rm_dispatched_by": "Administrator"},
			update_modified=False)
		frappe.db.set_value("Bin Inventory", cls.c3, {
			"rm_status": "Received", "rm_destination": cls.LINE, "rm_cycle": 1,
			"rm_received_at": now_datetime(), "rm_received_by": "Administrator",
			"rm_unplanned": 1, "rm_expected_destination": cls.OTHER},
			update_modified=False)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		cls._wipe()
		super().tearDownClass()

	@classmethod
	def _wipe(cls):
		for dt in ("Coil Transaction", "Bin Inventory"):
			for n in frappe.get_all(dt, filters={"coil_barcode": ["like", "__TEST__RMV%"]},
									pluck="name"):
				frappe.delete_doc(dt, n, force=1, ignore_permissions=True)
		frappe.db.commit()

	def _line(self, summary):
		return next(r for r in summary["rows"] if r["destination"] == self.LINE)

	# ---- rollup ----
	def test_summary_counts_in_transit_and_flagged(self):
		row = self._line(console_api.get_rm_summary())
		self.assertEqual(row["in_transit"], 2)
		self.assertEqual(row["flagged"], 1)

	def test_summary_ages_from_the_oldest_dispatch(self):
		"""A line's health is its oldest coil, not its newest — the newest always
		looks fine and would hide a coil that never arrived."""
		row = self._line(console_api.get_rm_summary(stale_hours=4))
		self.assertGreaterEqual(row["oldest_hours"], 8)
		self.assertEqual(row["stale"], 1)
		fresh = self._line(console_api.get_rm_summary(stale_hours=24))
		self.assertEqual(fresh["stale"], 0)

	def test_summary_skips_lines_with_nothing_to_report(self):
		dests = {r["destination"] for r in console_api.get_rm_summary()["rows"]}
		self.assertIn(self.LINE, dests)
		self.assertNotIn(self.OTHER, dests)   # only ever an *expected* destination

	# ---- coil list ----
	def test_movement_list_filters_by_line_and_state(self):
		out = console_api.get_rm_movement(destination=self.LINE)
		self.assertEqual({r["coil_barcode"] for r in out["rows"]},
						 {self.c1, self.c2, self.c3})
		transit = console_api.get_rm_movement(destination=self.LINE,
											  status="In Transit to Line")
		self.assertEqual({r["coil_barcode"] for r in transit["rows"]}, {self.c1, self.c2})

	def test_movement_list_finds_flagged_rows(self):
		out = console_api.get_rm_movement(flagged=1, q="__TEST__RMV")
		self.assertEqual([r["coil_barcode"] for r in out["rows"]], [self.c3])
		self.assertEqual(out["rows"][0]["rm_expected_destination"], self.OTHER)

	def test_movement_list_reports_age_for_in_transit_only(self):
		rows = {r["coil_barcode"]: r for r in
				console_api.get_rm_movement(destination=self.LINE)["rows"]}
		self.assertGreaterEqual(rows[self.c1]["age_hours"], 8)
		self.assertIsNone(rows[self.c3]["age_hours"])   # it arrived; age is meaningless

	def test_movement_total_matches_the_filters(self):
		"""The UI prints "showing N of M"; a filtered M that counts the whole table
		lies about how much is left."""
		out = console_api.get_rm_movement(destination=self.LINE, flagged=1)
		self.assertEqual(out["total"], 1)

	def test_history_returns_the_ledger_for_one_coil(self):
		coil = "__TEST__RMV_C4"
		_mkbin(coil, self.YARD, 1200)
		_mktxn(coil, "", "In-app", "RM Dispatch", now_datetime())
		out = console_api.get_rm_coil_history(coil)
		self.assertEqual([r.transaction_type for r in out["rows"]], ["RM Dispatch"])

	# ---- clearing a flag ----
	def test_clear_flag_drops_the_flag_but_not_the_coil(self):
		console_api.rm_clear_flag(self.c3)
		st = frappe.db.get_value("Bin Inventory", self.c3,
								 ["rm_unplanned", "rm_status", "rm_destination"], as_dict=True)
		self.assertEqual(st.rm_unplanned, 0)
		self.assertEqual(st.rm_status, "Received")
		self.assertEqual(st.rm_destination, self.LINE)
		# put it back for the other tests in this class
		frappe.db.set_value("Bin Inventory", self.c3,
							{"rm_unplanned": 1, "rm_expected_destination": self.OTHER},
							update_modified=False)
		frappe.db.commit()

	def test_rm_views_require_a_console_role(self):
		"""A line operator must not be able to clear the flag on their own exception."""
		if not frappe.db.exists("User", PLAIN_USER):
			u = frappe.new_doc("User")
			u.email = PLAIN_USER
			u.first_name = "Plain"
			u.user_type = "System User"
			u.insert(ignore_permissions=True)
			frappe.db.commit()
		frappe.set_user(PLAIN_USER)
		try:
			for call in (lambda: console_api.get_rm_summary(),
						 lambda: console_api.get_rm_movement(),
						 lambda: console_api.rm_clear_flag(self.c3)):
				with self.assertRaises(frappe.PermissionError):
					call()
		finally:
			frappe.set_user("Administrator")

	def test_export_carries_the_rm_rows(self):
		rows = console_api._export_rows("rm", {"destination": self.LINE})
		self.assertEqual({r["coil_barcode"] for r in rows}, {self.c1, self.c2, self.c3})
