"""Unit tests for RM Movement (plant gate -> production line).

Covers both legs, the offline-ordering guards that are the whole reason this
module has a state machine rather than two setters, the undo/reversal path, the
ledger reconciliation that survives a Bin Inventory row being deleted underneath
it, and the cross-flow isolation from Inventory Count.

The invariant asserted most often here: **RM never writes Bin Inventory.bin_code.**
The truck-loading flow reads that field to direct the picker, so a coil going to a
production line must keep its warehouse bin.

All data is __TEST__-prefixed and torn down per the prod-test convention (the
endpoints commit, which defeats FrappeTestCase's rollback). Run:

    bench --site <site> run-tests --app sbx_wmslite --module sbx_wmslite.tests.test_rm
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from sbx_wmslite import api, bin_api, count_api, rm_api

LINE_A = "__TEST__RM_LINE_A"
LINE_B = "__TEST__RM_LINE_B"
YARD = "__TEST__RM_YARD"

PREFIX = "__TEST__RM"


def _settings():
	s = frappe.get_doc("WMSLite Settings")
	s.rm_enabled = 1
	s.rm_received_history_hours = 12
	s.rm_unknown_bin = "__TEST__RM_UNKNOWN"
	s.count_enabled = 1
	s.qr_decode_rule = "Identity"
	s.bin_decode_rule = "Identity"
	s.save(ignore_permissions=True)
	frappe.db.commit()


def _cleanup():
	for dt in ("Coil Transaction", "Bin Inventory"):
		for n in frappe.get_all(dt, filters={"coil_barcode": ["like", PREFIX + "%"]},
								pluck="name"):
			frappe.delete_doc(dt, n, force=1, ignore_permissions=True)
	frappe.db.commit()


class TestRMMovement(FrappeTestCase):
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

	# ---- helpers ----
	def _coil(self, n=1):
		return f"{PREFIX}_C_{self._testMethodName}_{n}"

	def _seed(self, coil, bin_code=YARD, **kw):
		"""A coil the SAP feed already knows about, sitting in a warehouse bin."""
		data = {"bin_code": bin_code, "material_grade": "X52", "weight": 4200}
		data.update(kw)
		bin_api.upsert_bin(coil, data, source="Push API")
		frappe.db.commit()

	def _bi(self, coil, field):
		return frappe.db.get_value("Bin Inventory", coil, field)

	# ======================================================================
	# dispatch leg
	# ======================================================================
	def test_dispatch_sets_in_transit_and_never_touches_bin_code(self):
		coil = self._coil()
		self._seed(coil)
		r = rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		self.assertEqual(r["coil_id"], coil)
		self.assertEqual(r["rm_status"], rm_api.IN_TRANSIT)
		self.assertEqual(r["rm_destination"], LINE_A)
		self.assertEqual(r["rm_source_bin"], YARD)
		self.assertEqual(self._bi(coil, "rm_status"), rm_api.IN_TRANSIT)
		self.assertEqual(self._bi(coil, "rm_cycle"), 1)
		self.assertTrue(self._bi(coil, "rm_dispatched_at"))
		# THE invariant: the loading flow's view of this coil is unchanged.
		self.assertEqual(self._bi(coil, "bin_code"), YARD)
		self.assertEqual(self._bi(coil, "status"), "Available")

	def test_dispatch_ledger_row_shape(self):
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		txn = frappe.get_all("Coil Transaction", filters={"txn_key": "d1"},
							 fields=["source", "transaction_type", "bin_code", "processed",
									 "rm_destination", "rm_cycle", "reversed"])
		self.assertEqual(len(txn), 1)
		self.assertEqual(txn[0].source, "In-app")
		self.assertEqual(txn[0].transaction_type, rm_api.TXN_DISPATCH)
		self.assertEqual(txn[0].rm_destination, LINE_A)
		self.assertEqual(txn[0].rm_cycle, 1)
		self.assertEqual(txn[0].reversed, 0)
		# processed=1 so the derive sweep never sees it; bin_code blank so nothing
		# can mistake an RM row for a location fact.
		self.assertEqual(txn[0].processed, 1)
		self.assertFalse(txn[0].bin_code)

	def test_dispatch_idempotent(self):
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="dup1")
		r2 = rm_api.rm_dispatch(LINE_A, coil, client_event_id="dup1")
		self.assertTrue(r2.get("idempotent"))
		self.assertEqual(frappe.db.count("Coil Transaction", {"txn_key": "dup1"}), 1)

	def test_dispatch_same_destination_dedupes(self):
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="s1")
		r = rm_api.rm_dispatch(LINE_A, coil, client_event_id="s2")
		self.assertTrue(r.get("already_in_transit"))
		self.assertEqual(self._bi(coil, "rm_cycle"), 1)  # same movement, not a new one

	def test_dispatch_different_destination_reroutes(self):
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="r1")
		r = rm_api.rm_dispatch(LINE_B, coil, client_event_id="r2")
		self.assertEqual(r.get("rerouted_from"), LINE_A)
		self.assertEqual(self._bi(coil, "rm_destination"), LINE_B)
		self.assertEqual(self._bi(coil, "rm_cycle"), 1)  # a redirect, not a new trip

	def test_dispatch_unknown_coil_creates_placeholder_row(self):
		coil = self._coil()
		self.assertFalse(frappe.db.exists("Bin Inventory", coil))
		r = rm_api.rm_dispatch(LINE_A, coil, client_event_id="u1")
		self.assertEqual(r["rm_status"], rm_api.IN_TRANSIT)
		self.assertTrue(frappe.db.exists("Bin Inventory", coil))
		self.assertEqual(self._bi(coil, "bin_code"), "__TEST__RM_UNKNOWN")
		self.assertEqual(self._bi(coil, "source"), "In-app")
		# scanned_at MUST be null, or upsert_bin's staleness guard would let this
		# placeholder outrank the first real feed scan.
		self.assertIsNone(self._bi(coil, "scanned_at"))

	def test_dispatch_never_blanks_feed_attributes(self):
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="a1")
		self.assertEqual(self._bi(coil, "material_grade"), "X52")
		self.assertEqual(self._bi(coil, "weight"), 4200)

	def test_open_destination_lists_in_transit(self):
		c1, c2 = self._coil(1), self._coil(2)
		self._seed(c1)
		self._seed(c2)
		rm_api.rm_dispatch(LINE_A, c1, client_event_id="o1")
		rm_api.rm_dispatch(LINE_A, c2, client_event_id="o2")
		out = rm_api.rm_open_destination(LINE_A)
		self.assertEqual(out["destination"], LINE_A)
		self.assertEqual(out["count"], 2)
		self.assertEqual({c["coil_barcode"] for c in out["coils"]}, {c1, c2})

	# ======================================================================
	# receipt leg
	# ======================================================================
	def test_receive_expected_coil(self):
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		r = rm_api.rm_receive(LINE_A, coil, client_event_id="rc1")
		self.assertEqual(r["rm_status"], rm_api.RECEIVED)
		self.assertFalse(r.get("unplanned"))
		self.assertEqual(self._bi(coil, "rm_status"), rm_api.RECEIVED)
		self.assertTrue(self._bi(coil, "rm_received_at"))
		self.assertEqual(self._bi(coil, "rm_received_by"), "Administrator")
		# Still the invariant: received at a line, but the warehouse bin is intact.
		self.assertEqual(self._bi(coil, "bin_code"), YARD)

	def test_receive_idempotent(self):
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		rm_api.rm_receive(LINE_A, coil, client_event_id="rdup")
		r2 = rm_api.rm_receive(LINE_A, coil, client_event_id="rdup")
		self.assertTrue(r2.get("idempotent"))
		self.assertEqual(frappe.db.count("Coil Transaction", {"txn_key": "rdup"}), 1)

	def test_re_receive_writes_no_second_ledger_row(self):
		"""Two receivers can both be staring at the same expected list."""
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		rm_api.rm_receive(LINE_A, coil, client_event_id="rc1")
		r = rm_api.rm_receive(LINE_A, coil, client_event_id="rc2")
		self.assertTrue(r.get("already_received"))
		self.assertEqual(frappe.db.count("Coil Transaction", {
			"coil_barcode": coil, "transaction_type": rm_api.TXN_RECEIPT}), 1)

	def test_receive_never_dispatched_is_unplanned(self):
		coil = self._coil()
		self._seed(coil)
		r = rm_api.rm_receive(LINE_A, coil, client_event_id="np1")
		self.assertEqual(r.get("unplanned"), 1)
		self.assertEqual(r.get("reason"), "not_dispatched")
		self.assertEqual(self._bi(coil, "rm_status"), rm_api.RECEIVED)
		self.assertEqual(self._bi(coil, "rm_unplanned"), 1)

	def test_receive_misrouted_records_expected_destination(self):
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		r = rm_api.rm_receive(LINE_B, coil, client_event_id="mr1")
		self.assertEqual(r.get("unplanned"), 1)
		self.assertEqual(r.get("reason"), "misrouted")
		self.assertEqual(self._bi(coil, "rm_destination"), LINE_B)
		self.assertEqual(self._bi(coil, "rm_expected_destination"), LINE_A)

	def test_open_line_expected_and_recent(self):
		c1, c2 = self._coil(1), self._coil(2)
		self._seed(c1)
		self._seed(c2)
		rm_api.rm_dispatch(LINE_A, c1, client_event_id="d1")
		rm_api.rm_dispatch(LINE_A, c2, client_event_id="d2")
		rm_api.rm_receive(LINE_A, c2, client_event_id="rc2")
		out = rm_api.rm_open_line(LINE_A)
		self.assertEqual(out["destination"], LINE_A)
		self.assertEqual([c["coil_barcode"] for c in out["expected"]], [c1])
		self.assertEqual([c["coil_barcode"] for c in out["received_recent"]], [c2])

	def test_open_line_excludes_other_lines(self):
		c1, c2 = self._coil(1), self._coil(2)
		self._seed(c1)
		self._seed(c2)
		rm_api.rm_dispatch(LINE_A, c1, client_event_id="d1")
		rm_api.rm_dispatch(LINE_B, c2, client_event_id="d2")
		self.assertEqual([c["coil_barcode"] for c in rm_api.rm_open_line(LINE_A)["expected"]], [c1])
		self.assertEqual([c["coil_barcode"] for c in rm_api.rm_open_line(LINE_B)["expected"]], [c2])

	def test_received_history_window_is_honoured(self):
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		rm_api.rm_receive(LINE_A, coil, client_event_id="rc1")
		# age the receipt past the window
		frappe.db.set_value("Bin Inventory", coil, "rm_received_at",
							add_to_date(now_datetime(), hours=-48), update_modified=False)
		frappe.db.commit()
		self.assertEqual(rm_api.rm_open_line(LINE_A)["received_recent"], [])

	# ======================================================================
	# the offline-ordering guards — the reason this is a state machine
	# ======================================================================
	def test_replayed_dispatch_does_not_resurrect_a_received_coil(self):
		"""Gate offline, line online: the receipt happened first in real life."""
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		rm_api.rm_receive(LINE_A, coil, client_event_id="rc1")
		# the gate's scan happened BEFORE the receipt, but reaches us after it
		earlier = add_to_date(now_datetime(), minutes=-30)
		r = rm_api.rm_dispatch(LINE_A, coil, client_event_id="late1",
							   occurred_at=earlier, synced_offline=1)
		self.assertTrue(r.get("superseded"))
		self.assertEqual(self._bi(coil, "rm_status"), rm_api.RECEIVED)
		# and it is gone from the line's work list, not resurrected onto it
		self.assertNotIn(coil, [c["coil_barcode"] for c in rm_api.rm_open_line(LINE_A)["expected"]])
		# the audit trail still records that the gate scanned it
		self.assertTrue(frappe.db.exists("Coil Transaction", {"txn_key": "late1"}))

	def test_replayed_dispatch_to_another_line_also_cannot_regress(self):
		"""The guard is NOT destination-scoped, which is the whole point.

		Gate sent it to LINE_A, it physically turned up at LINE_B and was received
		there. A destination-scoped rule would happily mark a coil that is sitting
		at LINE_B as 'In Transit to LINE_A' — worse than the bug it guards.
		"""
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_receive(LINE_B, coil, client_event_id="rc1")  # unplanned arrival
		earlier = add_to_date(now_datetime(), minutes=-30)
		r = rm_api.rm_dispatch(LINE_A, coil, client_event_id="late1",
							   occurred_at=earlier, synced_offline=1)
		self.assertTrue(r.get("superseded"))
		self.assertEqual(self._bi(coil, "rm_status"), rm_api.RECEIVED)
		self.assertEqual(self._bi(coil, "rm_destination"), LINE_B)
		# the intended destination is recorded so the mismatch is visible
		self.assertEqual(self._bi(coil, "rm_expected_destination"), LINE_A)

	def test_live_dispatch_of_received_coil_starts_a_new_cycle(self):
		"""The guard must not block a coil's legitimate second trip."""
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		rm_api.rm_receive(LINE_A, coil, client_event_id="rc1")
		r = rm_api.rm_dispatch(LINE_B, coil, client_event_id="d2")  # live, not replayed
		self.assertFalse(r.get("superseded"))
		self.assertEqual(self._bi(coil, "rm_status"), rm_api.IN_TRANSIT)
		self.assertEqual(self._bi(coil, "rm_destination"), LINE_B)
		self.assertEqual(self._bi(coil, "rm_cycle"), 2)
		# the previous trip's receipt data is cleared, not left to mislead
		self.assertFalse(self._bi(coil, "rm_received_at"))
		self.assertEqual(self._bi(coil, "rm_unplanned"), 0)
		self.assertFalse(self._bi(coil, "rm_expected_destination"))

	def test_replayed_dispatch_newer_than_receipt_still_applies(self):
		"""An offline scan that genuinely happened after the receipt is a new trip."""
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		rm_api.rm_receive(LINE_A, coil, client_event_id="rc1")
		later = add_to_date(now_datetime(), minutes=30)
		r = rm_api.rm_dispatch(LINE_B, coil, client_event_id="d2",
							   occurred_at=later, synced_offline=1)
		self.assertFalse(r.get("superseded"))
		self.assertEqual(self._bi(coil, "rm_status"), rm_api.IN_TRANSIT)
		self.assertEqual(self._bi(coil, "rm_cycle"), 2)

	def test_replayed_receipt_after_redispatch_flags_conflict(self):
		"""Line offline; meanwhile the gate re-dispatched the coil elsewhere."""
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		rm_api.rm_dispatch(LINE_B, coil, client_event_id="d2")  # gate reroutes, live
		earlier = add_to_date(now_datetime(), minutes=-30)
		r = rm_api.rm_receive(LINE_A, coil, client_event_id="rc1",
							  occurred_at=earlier, synced_offline=1)
		# a human scanned it at LINE_A — strongest available evidence — but flagged
		self.assertEqual(r.get("conflict"), 1)
		self.assertEqual(r.get("reason"), "redispatched")
		self.assertEqual(self._bi(coil, "rm_status"), rm_api.RECEIVED)
		self.assertEqual(self._bi(coil, "rm_destination"), LINE_A)
		self.assertEqual(self._bi(coil, "rm_expected_destination"), LINE_B)

	# ======================================================================
	# undo / reversal
	# ======================================================================
	def test_undo_dispatch_clears_state_and_keeps_bin(self):
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		rm_api.rm_remove(coil, "dispatch", client_event_id="d1")
		self.assertFalse(self._bi(coil, "rm_status"))
		self.assertFalse(self._bi(coil, "rm_destination"))
		self.assertEqual(self._bi(coil, "bin_code"), YARD)
		# marked reversed, NOT deleted — the txn_key must stay claimed
		self.assertEqual(frappe.db.get_value("Coil Transaction", {"txn_key": "d1"}, "reversed"), 1)
		self.assertEqual(frappe.db.count("Coil Transaction", {"txn_key": "d1"}), 1)

	def test_undo_dispatch_writes_a_reversal_row(self):
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		rm_api.rm_remove(coil, "dispatch", client_event_id="d1")
		self.assertEqual(frappe.db.count("Coil Transaction", {
			"coil_barcode": coil, "transaction_type": rm_api.TXN_DISPATCH_REVERSAL}), 1)

	def test_undo_receipt_returns_to_in_transit(self):
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		rm_api.rm_receive(LINE_A, coil, client_event_id="rc1")
		rm_api.rm_remove(coil, "receive", client_event_id="rc1")
		self.assertEqual(self._bi(coil, "rm_status"), rm_api.IN_TRANSIT)
		self.assertEqual(self._bi(coil, "rm_destination"), LINE_A)
		self.assertFalse(self._bi(coil, "rm_received_at"))
		# back on the line's work list
		self.assertIn(coil, [c["coil_barcode"] for c in rm_api.rm_open_line(LINE_A)["expected"]])

	def test_undo_dispatch_refused_once_received(self):
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		rm_api.rm_receive(LINE_A, coil, client_event_id="rc1")
		with self.assertRaises(frappe.ValidationError) as cm:
			rm_api.rm_remove(coil, "dispatch", client_event_id="d1")
		# must be classified terminal, or the handheld retries it forever
		self.assertTrue(api._terminal_failure(cm.exception))
		self.assertEqual(self._bi(coil, "rm_status"), rm_api.RECEIVED)

	def test_undo_receipt_does_not_resurrect_a_reversed_dispatch(self):
		"""Undo the dispatch, then undo the receipt: In Transit must NOT come back.

		Restoring the receipt's snapshot verbatim would leave the coil in transit
		with no live dispatch behind it — stuck on the expected list forever with
		nothing to explain it.
		"""
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		rm_api.rm_receive(LINE_A, coil, client_event_id="rc1")
		# reverse the dispatch row directly (the receipt blocks rm_remove for it)
		frappe.db.set_value("Coil Transaction", {"txn_key": "d1"}, "reversed", 1,
							update_modified=False)
		frappe.db.commit()
		rm_api.rm_remove(coil, "receive", client_event_id="rc1")
		self.assertFalse(self._bi(coil, "rm_status"))
		self.assertNotIn(coil, [c["coil_barcode"] for c in rm_api.rm_open_line(LINE_A)["expected"]])

	def test_undo_dispatch_of_unknown_coil_drops_placeholder(self):
		coil = self._coil()
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		self.assertTrue(frappe.db.exists("Bin Inventory", coil))
		rm_api.rm_remove(coil, "dispatch", client_event_id="d1")
		self.assertFalse(frappe.db.exists("Bin Inventory", coil))

	def test_undo_does_not_drop_a_feed_row(self):
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		rm_api.rm_remove(coil, "dispatch", client_event_id="d1")
		self.assertTrue(frappe.db.exists("Bin Inventory", coil))
		self.assertEqual(self._bi(coil, "bin_code"), YARD)

	# ======================================================================
	# ledger reconciliation — Bin Inventory is an index, not the record
	# ======================================================================
	def test_expected_list_survives_bin_inventory_deletion(self):
		"""The SAP feed can hard-delete a Bin Inventory row mid-transit."""
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		frappe.delete_doc("Bin Inventory", coil, force=1, ignore_permissions=True)
		frappe.db.commit()
		out = rm_api.rm_open_line(LINE_A)
		rows = [c for c in out["expected"] if c["coil_barcode"] == coil]
		self.assertEqual(len(rows), 1, "in-transit coil vanished from the expected list")
		self.assertEqual(rows[0]["recovered"], 1)
		self.assertEqual(out["recovered_count"], 1)

	def test_feed_refuses_to_remove_an_in_transit_coil(self):
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		# Reuse the site's configured key rather than overwriting it — this suite
		# runs against prod, and clobbering sap_api_key would break the live feed.
		key = frappe.get_doc("WMSLite Settings").get_password("sap_api_key",
															 raise_exception=False)
		if not key:
			self.skipTest("sap_api_key not configured on this site")
		body = json.dumps({"action": "remove", "rows": [{"coil_barcode": coil}]})
		saved = getattr(frappe.local, "request", None)
		frappe.local.request = frappe._dict(
			get_data=lambda as_text=True: body,
			headers={"X-WMSLite-Key": key},
		)
		try:
			out = bin_api.receive_bin_inventory()
		finally:
			frappe.local.request = saved
		self.assertEqual(out.get("removed"), 0)
		self.assertTrue(any("in transit" in e for e in out.get("errors", [])),
						f"guard did not fire: {out}")
		self.assertTrue(frappe.db.exists("Bin Inventory", coil))

	def test_reconciliation_ignores_already_received(self):
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		rm_api.rm_receive(LINE_A, coil, client_event_id="rc1")
		out = rm_api.rm_open_line(LINE_A)
		self.assertEqual([c["coil_barcode"] for c in out["expected"]], [])

	# ======================================================================
	# cross-flow isolation from Inventory Count
	# ======================================================================
	def test_count_undo_does_not_touch_rm_ledger(self):
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		count_api.count_scan(LINE_A, coil, client_event_id="ct1")
		count_api.count_remove(LINE_A, coil, client_event_id="ct1")
		# the RM row is untouched by a count undo
		self.assertTrue(frappe.db.exists("Coil Transaction", {"txn_key": "d1"}))
		self.assertEqual(self._bi(coil, "rm_status"), rm_api.IN_TRANSIT)

	def test_count_rederive_never_blanks_bin_code_via_rm_row(self):
		"""An RM row carries no bin_code; _rederive_coil writes bin_code outright."""
		coil = self._coil()
		self._seed(coil)
		count_api.count_scan(YARD, coil, client_event_id="ct1")
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")  # newest row, blank bin
		count_api.count_remove(YARD, coil, client_event_id="ct1")
		self.assertTrue(self._bi(coil, "bin_code"), "bin_code was blanked by an RM row")

	def test_rm_rows_are_invisible_to_the_derive_sweep(self):
		coil = self._coil()
		self._seed(coil)
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1")
		bin_api.process_pending_transactions()
		self.assertEqual(self._bi(coil, "bin_code"), YARD)
		self.assertEqual(self._bi(coil, "source"), "Push API")

	# ======================================================================
	# guards
	# ======================================================================
	def test_my_tasks_reflects_rm_flag_and_roles(self):
		out = count_api.my_tasks()  # Administrator = System Manager -> everything
		self.assertIn("rm_dispatch", out["tasks"])
		self.assertIn("rm_receive", out["tasks"])
		frappe.db.set_single_value("WMSLite Settings", "rm_enabled", 0)
		try:
			tasks = count_api.my_tasks()["tasks"]
			self.assertNotIn("rm_dispatch", tasks)
			self.assertNotIn("rm_receive", tasks)
		finally:
			frappe.db.set_single_value("WMSLite Settings", "rm_enabled", 1)

	def test_role_enforced_on_every_endpoint(self):
		coil = self._coil()
		frappe.set_user("Guest")
		try:
			for fn, args in (
				(rm_api.rm_open_destination, (LINE_A,)),
				(rm_api.rm_dispatch, (LINE_A, coil)),
				(rm_api.rm_open_line, (LINE_A,)),
				(rm_api.rm_receive, (LINE_A, coil)),
				(rm_api.rm_remove, (coil, "dispatch")),
				(rm_api.rm_finish, (LINE_A,)),
			):
				with self.assertRaises(frappe.PermissionError):
					fn(*args)
		finally:
			frappe.set_user("Administrator")

	def test_disabled_flag_blocks_and_is_terminal(self):
		frappe.db.set_single_value("WMSLite Settings", "rm_enabled", 0)
		try:
			with self.assertRaises(frappe.ValidationError) as cm:
				rm_api.rm_dispatch(LINE_A, self._coil(), client_event_id="off1")
			self.assertTrue(api._terminal_failure(cm.exception))
		finally:
			frappe.db.set_single_value("WMSLite Settings", "rm_enabled", 1)

	def test_undecodable_coil_is_terminal(self):
		"""A garbage scan never becomes readable — it must not retry forever."""
		with self.assertRaises(frappe.ValidationError) as cm:
			rm_api.rm_dispatch(LINE_A, "   ", client_event_id="bad1")
		self.assertTrue(api._terminal_failure(cm.exception))

	def test_missing_destination_is_terminal(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			rm_api.rm_dispatch("", self._coil(), client_event_id="bad2")
		self.assertTrue(api._terminal_failure(cm.exception))

	def test_timestamps_come_from_the_server(self):
		coil = self._coil()
		self._seed(coil)
		bogus = "2001-01-01 00:00:00"
		rm_api.rm_dispatch(LINE_A, coil, client_event_id="d1", occurred_at=bogus)
		scanned = frappe.db.get_value("Coil Transaction", {"txn_key": "d1"}, "scanned_at")
		self.assertGreater(str(scanned), "2020", "device clock leaked into the ledger")
		self.assertGreater(str(self._bi(coil, "rm_dispatched_at")), "2020")

	# ======================================================================
	# offline replay through the queue
	# ======================================================================
	def test_offline_queue_replays_both_legs(self):
		coil = self._coil()
		self._seed(coil)
		occurred = str(add_to_date(now_datetime(), minutes=-10))
		events = [
			{"type": "rm_dispatch", "destination": LINE_A, "raw_qr": coil,
			 "coil_barcode": coil, "occurred_at": occurred, "client_event_id": "q1"},
			{"type": "rm_receive", "destination": LINE_A, "raw_qr": coil,
			 "coil_barcode": coil, "occurred_at": occurred, "client_event_id": "q2"},
			# a duplicate replay of the first must not double-insert
			{"type": "rm_dispatch", "destination": LINE_A, "raw_qr": coil,
			 "coil_barcode": coil, "occurred_at": occurred, "client_event_id": "q1"},
		]
		results = api.submit_offline_queue(json.dumps(events))
		self.assertTrue(all(r.get("ok") is not False for r in results),
						f"queue replay failed: {results}")
		self.assertEqual(frappe.db.count("Coil Transaction", {"txn_key": "q1"}), 1)
		self.assertEqual(self._bi(coil, "rm_status"), rm_api.RECEIVED)

	def test_offline_queue_unknown_rm_event_would_be_dropped(self):
		"""Documents why the dispatch table matters: unknown types are terminal."""
		results = api.submit_offline_queue(json.dumps([{"type": "rm_bogus",
													   "client_event_id": "z1"}]))
		self.assertFalse(results[0]["ok"])
		self.assertTrue(results[0]["terminal"])

	def test_offline_queue_failure_leaves_no_orphan_row(self):
		"""A throw mid-batch must not leave a processed=0 row for the sweep."""
		coil = self._coil()
		self._seed(coil)
		before = frappe.db.count("Coil Transaction", {"processed": 0})
		events = [
			{"type": "rm_dispatch", "destination": "", "raw_qr": coil,
			 "client_event_id": "f1"},                      # throws: no destination
			{"type": "rm_dispatch", "destination": LINE_A, "raw_qr": coil,
			 "client_event_id": "f2"},                      # succeeds, commits
		]
		results = api.submit_offline_queue(json.dumps(events))
		self.assertFalse(results[0]["ok"])
		self.assertTrue(results[0]["terminal"])
		self.assertTrue(results[1].get("ok"))
		self.assertEqual(frappe.db.count("Coil Transaction", {"processed": 0}), before)

	# ======================================================================
	# finish
	# ======================================================================
	def test_finish_reports_counts(self):
		c1, c2 = self._coil(1), self._coil(2)
		self._seed(c1)
		self._seed(c2)
		rm_api.rm_dispatch(LINE_A, c1, client_event_id="d1")
		rm_api.rm_dispatch(LINE_A, c2, client_event_id="d2")
		rm_api.rm_receive(LINE_A, c2, client_event_id="rc2")
		out = rm_api.rm_finish(LINE_A, "dispatch")
		self.assertEqual(out["in_transit"], 1)
		self.assertEqual(out["received"], 1)
