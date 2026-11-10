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

from sbx_wmslite import classify, console_api

BIN_A = "__TEST__BIN-A"
BIN_B = "__TEST__BIN-B"
# Location-master rows that hold no stock, so they can only come from WMSLite Bin.
MASTER_PREFIX = "__TEST__MASTER"
MASTER_ONLY = MASTER_PREFIX + "/EMPTY"
MASTER_OFF = MASTER_PREFIX + "/RETIRED"
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


def _mktxn(coil, bin_code, source, txn_type, when, product=None, rm_destination=None):
	key = f"__TEST__{coil}-{source}-{when}"
	if frappe.db.exists("Coil Transaction", {"txn_key": key}):
		return frappe.db.get_value("Coil Transaction", {"txn_key": key}, "name")
	d = frappe.new_doc("Coil Transaction")
	d.coil_barcode = coil
	d.bin_code = bin_code
	d.source = source
	d.transaction_type = txn_type
	d.scanned_at = when
	d.product = product
	d.rm_destination = rm_destination
	d.txn_key = key
	d.processed = 1
	d.raw_json = '{"BATCH": "%s", "LOCATION": "%s"}' % (coil, bin_code)
	d.insert(ignore_permissions=True)
	return d.name


def _mkmaster(code, disabled=0):
	"""A location master row. Deliberately holds no stock — that is the point."""
	if frappe.db.exists("WMSLite Bin", code):
		frappe.delete_doc("WMSLite Bin", code, force=1, ignore_permissions=True)
	d = frappe.new_doc("WMSLite Bin")
	d.bin_code = code
	d.zone = "__TEST__Z"
	d.disabled = disabled
	d.source = "Master"
	d.insert(ignore_permissions=True)
	return d.name


def _cleanup():
	frappe.set_user("Administrator")
	for dt, field in (("Coil Transaction", "txn_key"), ("Bin Inventory", "coil_barcode"),
					  ("WMSLite Bin", "bin_code"), ("Loading Plan", "truck_number"),
					  ("WMSLite Site", "name")):
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
		_mkmaster(MASTER_ONLY)
		_mkmaster(MASTER_OFF, disabled=1)
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

	def test_coil_weight_comes_from_the_qr_label(self):
		"""By Coil shows the weight printed on the QR (MT); a coil with no QR keeps
		its stored weight. The Excel drops Material and carries the raw QR."""
		qr = "1200 - GC Sheet-26AM272 - 0.300 mm  X 800 X 1830 mm - 2.800 MT- GCS0300"
		frappe.db.set_value("Bin Inventory", COILS[1], "coil_qr_raw", qr, update_modified=False)
		try:
			frappe.db.set_value("Bin Inventory", COILS[0], "sized", "0.250 mm X 780 X 2440 mm", update_modified=False)
			rows = {r["coil_barcode"]: r for r in console_api.get_bin_inventory(bin_code=BIN_A)["rows"]}
			self.assertEqual((rows[COILS[1]]["weight"], rows[COILS[1]]["weight_source"]), (2.8, "QR"))
			self.assertEqual((float(rows[COILS[0]]["weight"]), rows[COILS[0]]["weight_source"]), (1000.0, "Stored"))
			# size: off the QR (4th '-' part); a coil with no QR keeps its SAP size
			self.assertEqual((rows[COILS[1]]["size"], rows[COILS[1]]["size_source"]),
							 ("0.300 mm X 800 X 1830 mm", "QR"))
			self.assertEqual((rows[COILS[0]]["size"], rows[COILS[0]]["size_source"]),
							 ("0.250 mm X 780 X 2440 mm", "Stored"))

			_p, header, body = self._sheet(console_api.export_view, view="inventory_coil", bin_code=BIN_A)
			self.assertNotIn("Material", header)
			self.assertIn("Weight (MT)", header)
			self.assertEqual(header[-1], "Raw QR")
			by_coil = {r[1]: r for r in body if r and r[1]}
			self.assertEqual(by_coil[COILS[1]][-1], qr)
			self.assertEqual(float(by_coil[COILS[1]][header.index("Weight (MT)")]), 2.8)
			self.assertEqual(header.index("Size"), header.index("Weight (MT)") + 1)
			self.assertEqual(by_coil[COILS[1]][header.index("Size")], "0.300 mm X 800 X 1830 mm")
			self.assertIn(by_coil[COILS[0]][-1], (None, ""))
		finally:
			frappe.db.set_value("Bin Inventory", COILS[1], "coil_qr_raw", None, update_modified=False)
			frappe.db.set_value("Bin Inventory", COILS[0], "sized", None, update_modified=False)

	# ---- bin master (WMSLite Bin, not Bin Inventory) ----
	def test_bin_master_lists_locations_with_no_stock(self):
		"""The point of this view: Inventory can only show bins that hold coils."""
		d = console_api.get_bin_master(q=MASTER_ONLY)
		self.assertEqual([r["bin_code"] for r in d["rows"]], [MASTER_ONLY])
		self.assertEqual(d["total"], 1)
		# ... and that bin holds nothing, so the rollup has never heard of it
		self.assertEqual(console_api.get_bin_rollup(q=MASTER_ONLY)["rows"], [])

	def test_bin_master_filters_by_disabled(self):
		self.assertEqual(console_api.get_bin_master(q=MASTER_PREFIX, disabled=1)["total"], 1)
		self.assertEqual(console_api.get_bin_master(q=MASTER_PREFIX, disabled=0)["total"], 1)
		self.assertEqual(console_api.get_bin_master(q=MASTER_PREFIX)["total"], 2)

	def test_bin_master_export_labels_map_back_to_column_names(self):
		"""The download-edit-reupload loop depends on these exact labels."""
		_p, header, body = self._sheet(console_api.export_view, view="bin_master", q=MASTER_PREFIX)
		# the dataset columns first, verbatim; derived extras (Site) trail and are
		# ignored by the upload mapping
		self.assertEqual(header[:6], ["Bin Code", "Zone", "Aisle", "Is Line", "Disabled", "Source"])
		self.assertEqual(header[6:], ["Site"])
		self.assertEqual({r[0] for r in body if r and r[0]}, {MASTER_ONLY, MASTER_OFF})
		spec = console_api.DATASETS["Bin Master"]
		mapped = [console_api._norm_header(h) for h in header]
		for col in spec["columns"]:
			self.assertIn(col, mapped)

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


class TestConsoleGlobalFilters(FrappeTestCase):
	"""The console's global Site / Material Type filters and the dashboard's
	Transaction Type groups.

	Site and material are DERIVED (bin prefix via the WMSLite Site master; product
	keyword), so what is pinned here is that every list endpoint agrees on the
	derivation: a handheld row with no bin and no product resolves through its RM
	destination and its coil's inventory record, a truck resolves through its SAP
	plant code, and the "Unassigned" bucket really catches the rest.
	"""

	P = "__TEST__GF"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		frappe.flags.wms_skip_backfill = True
		_cleanup()
		classify.seed_sites()
		classify.clear_cache()
		P = cls.P
		cls.bin_j, cls.bin_r, cls.bin_p, cls.bin_u = f"JPR/{P}/A", f"CC2/{P}/B", f"RP/{P}/C", f"PKL3/{P}/D"
		cls.line = f"JPR/{P}/LINE"
		cls.c_j, cls.c_r, cls.c_p, cls.c_u = f"{P}_J1", f"{P}_R1", f"{P}_P1", f"{P}_U1"
		for coil, bin_code, product in ((cls.c_j, cls.bin_j, "H.R.COIL"), (cls.c_r, cls.bin_r, "GC Sheet"),
										(cls.c_p, cls.bin_p, "GI PIPE"), (cls.c_u, cls.bin_u, None)):
			_mkbin(coil, bin_code, 100)
			frappe.db.set_value("Bin Inventory", coil, "product", product, update_modified=False)
		_mkmaster(cls.bin_j)
		_mkmaster(cls.bin_u)
		# feed row with a product; count row with none (borrows the coil's); RM rows with no bin
		cls.t_feed = _mktxn(cls.c_j, cls.bin_j, "Push API", None, "2026-08-01 10:00:00", product="H.R.COIL")
		cls.t_count = _mktxn(cls.c_p, cls.bin_p, "In-app", "Inventory Count", "2026-08-02 10:00:00")
		cls.t_rm_d = _mktxn(cls.c_r, "", "In-app", "RM Dispatch", "2026-08-03 09:00:00", rm_destination=cls.line)
		cls.t_rm_r = _mktxn(cls.c_r, "", "In-app", "RM Receipt", "2026-08-03 10:00:00", rm_destination=cls.line)
		# the coil's live RM state, as rm_receive would leave it (flagged)
		frappe.db.set_value("Bin Inventory", cls.c_r, {
			"rm_status": "Received", "rm_destination": cls.line, "rm_cycle": 1, "rm_unplanned": 1,
			"rm_received_at": now_datetime(), "rm_received_by": "Administrator"}, update_modified=False)
		# trucks: site comes from the SAP plant code
		cls.t5100 = cls._plan(f"{P}_T5100", "5100")
		cls.t1300 = cls._plan(f"{P}_T1300", "1300")
		cls.tnone = cls._plan(f"{P}_TNONE", None)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		_cleanup()
		frappe.flags.wms_skip_backfill = False
		super().tearDownClass()

	@classmethod
	def _plan(cls, truck, source_plant):
		p = frappe.new_doc("Loading Plan")
		p.truck_number = truck
		p.sap_plan_id = truck
		p.source_plant = source_plant
		# "Sent" so the loading-push sweep never considers this coil live
		p.append("coils", {"coil_barcode": truck + "_C", "weight": 500, "material_grade": "GR50",
						   "coil_status": "Loaded", "sap_loading_status": "Sent"})
		p.insert(ignore_permissions=True)
		return p.name

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_filter_options(self):
		o = console_api.get_filter_options()
		self.assertEqual(o["sites"][:3], ["Jangalpur", "Ranihati", "Pipe Division"])
		self.assertEqual(o["sites"][-1], classify.UNASSIGNED)
		self.assertEqual(o["material_types"], ["Sheet (Coil)", "Pipes", "Unassigned"])
		self.assertEqual([k for k, _l in o["txn_types"]], ["", "inventory", "rm", "loading"])
		self.assertEqual(o["support"]["trucks"], ["site"])
		self.assertEqual(o["support"]["saplog"], [])

	def test_site_filter_on_inventory_views(self):
		P = self.P
		rollup = lambda **kw: {r["bin_code"]: r for r in console_api.get_bin_rollup(q=P, **kw)["rows"]}
		self.assertEqual(set(rollup(site="Jangalpur")), {self.bin_j})
		self.assertEqual(rollup(site="Jangalpur")[self.bin_j]["site"], "Jangalpur")
		self.assertEqual(set(rollup(site="Ranihati")), {self.bin_r})
		self.assertEqual(set(rollup(site="Pipe Division")), {self.bin_p})
		self.assertEqual(set(rollup(site=classify.UNASSIGNED)), {self.bin_u})
		self.assertEqual(set(rollup()), {self.bin_j, self.bin_r, self.bin_p, self.bin_u})
		self.assertEqual(console_api.get_bin_rollup(q=P, site="Jangalpur")["total"], 1)

		coils = console_api.get_bin_inventory(q=P, site="Pipe Division")
		self.assertEqual([r["coil_barcode"] for r in coils["rows"]], [self.c_p])
		self.assertEqual((coils["rows"][0]["site"], coils["rows"][0]["material_type"]), ("Pipe Division", "Pipes"))
		self.assertEqual(coils["total"], 1)

		master = console_api.get_bin_master(q="JPR/" + P, site="Jangalpur")
		self.assertEqual([r["bin_code"] for r in master["rows"]], [self.bin_j])
		self.assertEqual(master["rows"][0]["site"], "Jangalpur")
		self.assertEqual(console_api.get_bin_master(q="JPR/" + P, site="Ranihati")["total"], 0)
		self.assertEqual([r["bin_code"] for r in console_api.get_bin_master(q="PKL3/" + P, site=classify.UNASSIGNED)["rows"]],
						 [self.bin_u])

	def test_material_filter_on_inventory(self):
		P = self.P
		coils = lambda mt: {r["coil_barcode"] for r in console_api.get_bin_inventory(q=P, material_type=mt)["rows"]}
		self.assertEqual(coils("Pipes"), {self.c_p})
		self.assertEqual(coils("Sheet (Coil)"), {self.c_j, self.c_r})
		self.assertEqual(coils(classify.UNASSIGNED), {self.c_u})
		self.assertEqual(console_api.get_bin_inventory(q=P, site="Ranihati", material_type="Pipes")["total"], 0)
		d = console_api.get_dashboard(txn_type="inventory", site="Pipe Division", material_type="Pipes")
		self.assertGreaterEqual(d["inventory"]["coils"], 1)
		self.assertEqual(console_api.get_dashboard(txn_type="inventory", site="Ranihati", material_type="Pipes")
						 ["inventory"]["coils"], 0)

	def test_transactions_resolve_site_and_material_without_bin_or_product(self):
		P = self.P
		rows = lambda **kw: {r["name"] for r in console_api.get_coil_transactions(q=P, **kw)["rows"]}
		self.assertEqual(rows(site="Pipe Division"), {self.t_count})          # bin prefix
		self.assertEqual(rows(site="Jangalpur"), {self.t_feed, self.t_rm_d, self.t_rm_r})   # feed bin; RM destination
		self.assertEqual(rows(site="Ranihati"), set())
		self.assertEqual(rows(material_type="Pipes"), {self.t_count})          # product borrowed from Bin Inventory
		self.assertEqual(rows(material_type="Sheet (Coil)"), {self.t_feed, self.t_rm_d, self.t_rm_r})
		r = console_api.get_coil_transactions(q=P, site="Pipe Division")
		self.assertEqual((r["rows"][0]["site"], r["rows"][0]["material_type"]), ("Pipe Division", "Pipes"))
		# the stats tiles follow the same filter
		self.assertGreaterEqual(r["stats"]["total"], 1)
		self.assertEqual(console_api.get_coil_transactions(q=P, site="Ranihati")["stats"]["total"],
						 console_api._scan_stats(site="Ranihati")["total"])

	def test_rm_views_honour_site(self):
		P = self.P
		s = console_api.get_rm_summary(site="Jangalpur")
		self.assertIn(self.line, [r["destination"] for r in s["rows"]])
		self.assertGreaterEqual(s["totals"]["flagged"], 1)
		self.assertNotIn(self.line, [r["destination"] for r in console_api.get_rm_summary(site="Ranihati")["rows"]])
		self.assertEqual([r["coil_barcode"] for r in console_api.get_rm_movement(q=P, site="Jangalpur")["rows"]], [self.c_r])
		self.assertEqual(console_api.get_rm_movement(q=P, site="Ranihati")["total"], 0)
		self.assertEqual(console_api.get_rm_movement(q=P, site="Jangalpur", material_type="Pipes")["total"], 0)

	def test_loading_views_use_the_plant_code(self):
		P = self.P
		trucks = lambda site: {r["truck_number"] for r in console_api.get_trucks(q=P, site=site)}
		self.assertEqual(trucks("Ranihati"), {f"{P}_T5100"})
		self.assertEqual(trucks(classify.UNASSIGNED), {f"{P}_T1300", f"{P}_TNONE"})   # 1300 is unmapped on purpose
		self.assertEqual(trucks("Jangalpur"), set())
		self.assertEqual(trucks(None), {f"{P}_T5100", f"{P}_T1300", f"{P}_TNONE"})
		loading = {r["truck_number"] for r in console_api.get_coil_loading(truck=P, site="Ranihati")}
		self.assertEqual(loading, {f"{P}_T5100"})
		self.assertEqual(console_api.get_coil_loading(truck=P, site="Jangalpur"), [])
		self.assertEqual(console_api.get_confirmations(site="Jangalpur"), [])

	def test_dashboard_groups(self):
		d = console_api.get_dashboard(txn_type="loading")
		self.assertIn("live", d)
		self.assertNotIn("scans", d)
		self.assertNotIn("rm", d)
		self.assertEqual(d["groups"], ["loading"])
		d = console_api.get_dashboard(txn_type="rm")
		self.assertEqual(set(d) - {"groups", "filters"}, {"rm"})
		d = console_api.get_dashboard()
		for k in ("live", "throughput", "coils", "inventory", "scans", "rm"):
			self.assertIn(k, d)
		self.assertEqual(d["groups"], ["loading", "inventory", "rm"])
		# RM tiles: the seeded flagged receipt at a Jangalpur line, moves on the 3rd
		rm = console_api.get_dashboard(txn_type="rm", site="Jangalpur", date_from="2026-08-03", date_to="2026-08-03")["rm"]
		self.assertGreaterEqual(rm["flagged"], 1)
		self.assertGreaterEqual(rm["dispatched"], 1)
		self.assertGreaterEqual(rm["received"], 1)
		self.assertEqual(console_api.get_dashboard(txn_type="rm", site="Ranihati", date_from="2026-08-03",
												   date_to="2026-08-03")["rm"]["received"], 0)
		# the scan tiles leave RM ledger rows out (they have their own tiles)
		scans = console_api.get_dashboard(txn_type="inventory", site="Jangalpur", date_from="2026-08-03",
										  date_to="2026-08-03")["scans"]["total"]
		listed = console_api.get_coil_transactions(site="Jangalpur", date_from="2026-08-03",
												   date_to="2026-08-03")["stats"]["total"]
		self.assertEqual(listed - scans, 2)
		# a site with no plans: loading tiles are zero, not an error
		self.assertEqual(console_api.get_dashboard(txn_type="loading", site="Pipe Division")["live"]["open"], 0)

	def test_export_header_states_the_global_filters(self):
		import io

		import openpyxl

		def sheet(**kw):
			frappe.response.clear()
			console_api.export_view(**kw)
			ws = openpyxl.load_workbook(io.BytesIO(frappe.response["filecontent"])).active
			rows = [list(r) for r in ws.iter_rows(values_only=True)]
			blank = next(i for i, r in enumerate(rows) if not any(c not in (None, "") for c in r))
			return rows[:blank], rows[blank + 1], rows[blank + 2:]
		pre, header, body = sheet(view="transactions", q=self.P, site="Jangalpur", material_type="Sheet (Coil)")
		note = next(r[0] for r in pre if r and str(r[0] or "").startswith("Filters:"))
		self.assertIn("site=Jangalpur", note)
		self.assertIn("material=Sheet (Coil)", note)
		self.assertIn("Site", header)
		self.assertEqual({r[header.index("Site")] for r in body if r and r[1]}, {"Jangalpur"})
		# a filter the view does not query on is not echoed as if it had applied
		pre, header, _b = sheet(view="trucks", q=self.P, site="Ranihati", material_type="Pipes")
		note = next(r[0] for r in pre if r and str(r[0] or "").startswith("Filters:"))
		self.assertIn("site=Ranihati", note)
		self.assertNotIn("material=", note)
		# the dashboard export names its section and filters
		frappe.response.clear()
		console_api.export_dashboard(txn_type="rm", site="Jangalpur")
		ws = openpyxl.load_workbook(io.BytesIO(frappe.response["filecontent"])).active
		text = "\n".join(str(r[0] or "") for r in ws.iter_rows(values_only=True))
		self.assertIn("section=RM Movement", text)
		self.assertIn("site=Jangalpur", text)
		self.assertNotIn("Coils in stock", text)

	def test_filter_options_require_console_role(self):
		if not frappe.db.exists("User", PLAIN_USER):
			u = frappe.new_doc("User")
			u.email = PLAIN_USER
			u.first_name = "Plain"
			u.user_type = "System User"
			u.send_welcome_email = 0
			u.insert(ignore_permissions=True)
			frappe.db.commit()
		frappe.set_user(PLAIN_USER)
		with self.assertRaises(frappe.PermissionError):
			console_api.get_filter_options()
		with self.assertRaises(frappe.PermissionError):
			console_api.get_dashboard(txn_type="rm")
