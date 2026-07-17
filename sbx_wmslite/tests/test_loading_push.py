"""Unit tests for the outbound SAP loading-transaction push (mocked HTTP).

The CSRF/HTTP seams are mocked, so these tests NEVER call a real SAP endpoint.
All data is __TEST__-prefixed and torn down; loading_push_enabled is reset to 0.

    bench --site <site> run-tests --app sbx_wmslite --module sbx_wmslite.tests.test_loading_push
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime

from sbx_wmslite import loading_push as lp

TRUCK = "__TEST__LPUSH"


class FakeResp:
	def __init__(self, status_code, text):
		self.status_code = status_code
		self.text = text


def _settings(enabled=1):
	s = frappe.get_doc("WMSLite Settings")
	s.loading_push_enabled = enabled
	s.loading_push_url = "https://sap.example.invalid/sap/bc/zpwmsb/rest"
	s.loading_push_user = "jindal1"
	s.loading_txn_type = "T1"
	s.loading_location_sep = "_"
	s.loading_push_batch_size = 200
	s.verify_tls = 0
	# Deterministic baseline: auto-complete OFF (the shipped default). Tests that
	# need it ON flip it explicitly.
	s.auto_complete_on_full_load = 0
	s.save(ignore_permissions=True)
	frappe.db.commit()


def _cleanup():
	for n in frappe.get_all("WMSLite SAP Loading", filters={"coil_barcode": ["like", "__TEST__%"]}, pluck="name"):
		frappe.delete_doc("WMSLite SAP Loading", n, force=1, ignore_permissions=True)
	for n in frappe.get_all("Loading Plan", filters={"truck_number": ["like", "__TEST__%"]}, pluck="name"):
		frappe.delete_doc("Loading Plan", n, force=1, ignore_permissions=True)
	frappe.db.commit()


class TestLoadingPush(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_cleanup()

	@classmethod
	def tearDownClass(cls):
		_cleanup()
		frappe.db.set_single_value("WMSLite Settings", "loading_push_enabled", 0)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		_cleanup()
		_settings(1)
		self.truck = TRUCK + "_" + self._testMethodName

	def _plan(self, codes, coil_status="Loaded", sap_status="Pending"):
		p = frappe.new_doc("Loading Plan")
		p.truck_number = self.truck
		p.sap_plan_id = "SHIP-" + self._testMethodName
		p.source_plant = "1300"
		for c in codes:
			p.append("coils", {
				"coil_barcode": c, "weight": 500, "material_grade": "GR50",
				"coil_status": coil_status, "loaded_by": "Administrator",
				"loaded_at": get_datetime("2026-06-27 12:30:45"),
				"sap_loading_status": sap_status})
		p.insert(ignore_permissions=True)
		frappe.db.commit()
		return p

	def _set_autocomplete(self, val):
		frappe.db.set_single_value("WMSLite Settings", "auto_complete_on_full_load", val)
		frappe.db.commit()

	def _statuses(self):
		name = frappe.get_all("Loading Plan", filters={"truck_number": self.truck}, pluck="name")[0]
		return {c.coil_barcode: c.sap_loading_status for c in frappe.get_doc("Loading Plan", name).coils}

	# ---- payload mapping ----
	def test_build_row(self):
		p = self._plan(["__TEST__BR1"])
		s = frappe.get_doc("WMSLite Settings")
		row = lp.build_row(p.coils[0], p, {"plant": "1300", "product": "Galvalume", "sized": "1.2"}, s)
		self.assertEqual(row["TRANSACTIONTYPE"], "T1")
		self.assertEqual(row["BATCH"], "__TEST__BR1")
		self.assertEqual(row["LOCATION"], self.truck)  # truck number only (no shipment)
		self.assertEqual(row["ENTRYDATE"], "27062026")
		self.assertEqual(row["ENTRYTIME"], "123045")
		self.assertEqual(row["USERNAME"], "jindal1")
		self.assertEqual(row["PLANT"], "1300")
		self.assertEqual(row["PRODUCT"], "Galvalume")
		self.assertEqual(row["QC"], "")

	# ---- QR parsing (port of the AppSheet formula) ----
	def test_parse_full_qr(self):
		r = lp.parse_inventory_barcode(
			"Jangalpur Al Foil - ALUMINIUM FOIL BARE-J2603B111 - 6.000 mm  X 1280 - 720.500 KG- UL06000")
		self.assertEqual(r["plant"], "Jangalpur Al Foil")
		self.assertEqual(r["product"], "ALUMINIUM FOIL BARE")
		self.assertEqual(r["batch"], "J2603B111")
		self.assertEqual(r["sized"], "6.000 mm X 1280")
		self.assertEqual(r["weight"], "720.500")       # KG stripped
		self.assertEqual(r["material"], "UL06000")

	def test_parse_extra_code_shifts_batch(self):
		r = lp.parse_inventory_barcode(
			"Vasind - Galvalume - OEM123 - 2R6C12356 - 1.2 mm X 1000 - 500.5 KG - GCS0250")
		self.assertEqual(r["batch"], "2R6C12356")       # shifted past the OEM extra-code
		self.assertEqual(r["sized"], "1.2 mm X 1000")
		self.assertEqual(r["weight"], "500.5")
		self.assertEqual(r["material"], "GCS0250")

	def test_parse_plain_barcode_returns_empty(self):
		self.assertEqual(lp.parse_inventory_barcode("2R6C12356"), {})
		self.assertEqual(lp.parse_inventory_barcode(""), {})
		self.assertEqual(lp.parse_inventory_barcode(None), {})

	def test_build_row_uses_qr_when_present(self):
		coil = frappe._dict({
			"coil_barcode": "J2603B111", "weight": 720.5, "material_grade": "GR50",
			"loaded_at": get_datetime("2026-06-27 12:30:45"),
			"coil_qr_raw": "Jangalpur Al Foil - ALUMINIUM FOIL BARE-J2603B111 - 6.000 mm X 1280 - 720.500 KG- UL06000"})
		plan = frappe._dict({"truck_number": self.truck, "sap_plan_id": "X", "source_plant": "1300"})
		row = lp.build_row(coil, plan, None, frappe.get_doc("WMSLite Settings"))
		self.assertEqual(row["PLANT"], "Jangalpur Al Foil")   # from QR, not source_plant
		self.assertEqual(row["PRODUCT"], "ALUMINIUM FOIL BARE")
		self.assertEqual(row["MATERIAL"], "UL06000")           # QR wins over material_grade
		self.assertEqual(row["SIZED"], "6.000 mm X 1280")
		self.assertEqual(row["BATCH"], "J2603B111")

	def test_build_row_falls_back_without_qr(self):
		coil = frappe._dict({
			"coil_barcode": "2R6C12356", "weight": 500, "material_grade": "GR50",
			"loaded_at": get_datetime("2026-06-27 12:30:45"), "coil_qr_raw": "2R6C12356"})
		plan = frappe._dict({"truck_number": self.truck, "sap_plan_id": "X", "source_plant": "1300"})
		row = lp.build_row(coil, plan, None, frappe.get_doc("WMSLite Settings"))
		self.assertEqual(row["PLANT"], "1300")                 # plain barcode → source_plant fallback
		self.assertEqual(row["MATERIAL"], "GR50")

	def test_build_row_plant_sentinel_when_nothing(self):
		# plain-barcode scan, plan has no source_plant, no bin → sentinel plant
		coil = frappe._dict({
			"coil_barcode": "J1909A701", "weight": 480, "material_grade": "GR50",
			"loaded_at": get_datetime("2026-06-27 12:30:45"), "coil_qr_raw": "J1909A701"})
		plan = frappe._dict({"truck_number": self.truck, "sap_plan_id": "X", "source_plant": None})
		row = lp.build_row(coil, plan, None, frappe.get_doc("WMSLite Settings"))
		self.assertEqual(row["PLANT"], lp.NO_SCAN_PLANT)       # "Coil Barcode not scanned"

	# ---- successful push ----
	def test_sweep_success_marks_sent(self):
		self._plan(["__TEST__S1", "__TEST__S2"])

		def fake_post(session, url, data, headers, timeout, verify):
			rows = json.loads(data)
			out = [{"BATCH": r["BATCH"], "ENTRYDATE": r["ENTRYDATE"],
					"ENTRYTIME": r["ENTRYTIME"], "MESSAGE": "Data Pushed Successfully"} for r in rows]
			return FakeResp(200, json.dumps(out))

		with patch.object(lp, "_csrf_token", return_value="TKN"), \
				patch.object(lp, "_http_post", side_effect=fake_post):
			res = lp.sweep()
		self.assertEqual(res["sent"], 2)
		self.assertTrue(all(v == "Sent" for v in self._statuses().values()))

	# ---- business failure parks just that coil ----
	def test_sweep_business_failure_marks_failed(self):
		self._plan(["__TEST__F1", "__TEST__F2"])

		def fake_post(session, url, data, headers, timeout, verify):
			# Key success on the barcode, not list position — the batch order for
			# coils sharing a loaded_at is not deterministic.
			rows = json.loads(data)
			out = []
			for r in rows:
				msg = "Data Pushed Successfully" if r["BATCH"] == "__TEST__F1" else "Batch not found in SAP"
				out.append({"BATCH": r["BATCH"], "ENTRYDATE": r["ENTRYDATE"],
							"ENTRYTIME": r["ENTRYTIME"], "MESSAGE": msg})
			return FakeResp(200, json.dumps(out))

		with patch.object(lp, "_csrf_token", return_value="TKN"), \
				patch.object(lp, "_http_post", side_effect=fake_post):
			lp.sweep()
		by = self._statuses()
		self.assertEqual(by["__TEST__F1"], "Sent")
		self.assertEqual(by["__TEST__F2"], "Failed")

	# ---- transport / CSRF failure leaves Pending for retry ----
	def test_sweep_transport_failure_keeps_pending(self):
		self._plan(["__TEST__T1"])
		with patch.object(lp, "_csrf_token", side_effect=ValueError("CSRF fetch failed")):
			lp.sweep()
		self.assertEqual(self._statuses()["__TEST__T1"], "Pending")

	# ---- server 5xx leaves Pending for retry ----
	def test_sweep_http_500_keeps_pending(self):
		self._plan(["__TEST__H1"])
		with patch.object(lp, "_csrf_token", return_value="TKN"), \
				patch.object(lp, "_http_post", return_value=FakeResp(503, "Service Unavailable")):
			lp.sweep()
		self.assertEqual(self._statuses()["__TEST__H1"], "Pending")

	# ---- disabled = no-op ----
	def test_disabled_noop(self):
		_settings(0)
		self._plan(["__TEST__D1"])
		res = lp.sweep()
		self.assertEqual(res.get("skipped"), "disabled")
		self.assertEqual(self._statuses()["__TEST__D1"], "Pending")

	# ---- requeue: a Failed coil is re-armed to Pending + a sweep is enqueued ----
	def _coil_name(self, barcode):
		name = frappe.get_all("Loading Plan", filters={"truck_number": self.truck}, pluck="name")[0]
		return frappe.get_all("Loading Plan Coil",
							  filters={"parent": name, "coil_barcode": barcode}, pluck="name")[0]

	def test_requeue_failed_rearms_pending(self):
		self._plan(["__TEST__RQ1"])
		cn = self._coil_name("__TEST__RQ1")
		frappe.db.set_value("Loading Plan Coil", cn, "sap_loading_status", "Failed")
		frappe.db.commit()
		with patch.object(frappe, "enqueue") as enq:
			res = lp.requeue(cn)
		self.assertTrue(res.get("ok"))
		self.assertEqual(self._statuses()["__TEST__RQ1"], "Pending")
		enq.assert_called_once()

	def test_requeue_skips_non_failed(self):
		self._plan(["__TEST__RQ2"])
		cn = self._coil_name("__TEST__RQ2")
		frappe.db.set_value("Loading Plan Coil", cn, "sap_loading_status", "Sent")
		frappe.db.commit()
		with patch.object(frappe, "enqueue") as enq:
			res = lp.requeue(cn)
		self.assertEqual(res.get("skipped"), "Sent")
		self.assertEqual(self._statuses()["__TEST__RQ2"], "Sent")
		enq.assert_not_called()

	def test_requeue_disabled_noop(self):
		self._plan(["__TEST__RQ3"])
		cn = self._coil_name("__TEST__RQ3")
		frappe.db.set_value("Loading Plan Coil", cn, "sap_loading_status", "Failed")
		frappe.db.commit()
		_settings(0)
		with patch.object(frappe, "enqueue") as enq:
			res = lp.requeue(cn)
		self.assertEqual(res.get("skipped"), "disabled")
		self.assertEqual(self._statuses()["__TEST__RQ3"], "Failed")
		enq.assert_not_called()

	# ---- tracking doctype: WMSLite SAP Loading ----
	def _record(self, barcode):
		cn = self._coil_name(barcode)
		name = frappe.db.get_value("WMSLite SAP Loading", {"coil": cn}, "name")
		return frappe.get_doc("WMSLite SAP Loading", name) if name else None

	def test_push_creates_tracking_record(self):
		self._plan(["__TEST__TR1"])

		def fake_post(session, url, data, headers, timeout, verify):
			rows = json.loads(data)
			out = [{"BATCH": r["BATCH"], "ENTRYDATE": r["ENTRYDATE"],
					"ENTRYTIME": r["ENTRYTIME"], "MESSAGE": "Data Pushed Successfully"} for r in rows]
			return FakeResp(200, json.dumps(out))

		with patch.object(lp, "_csrf_token", return_value="TKN"), \
				patch.object(lp, "_http_post", side_effect=fake_post):
			lp.sweep()
		rec = self._record("__TEST__TR1")
		self.assertIsNotNone(rec)
		self.assertEqual(rec.status, "Sent")
		self.assertEqual(rec.attempts, 1)
		self.assertIn("Data Pushed Successfully", rec.sap_message)
		self.assertIn("__TEST__TR1", rec.request_payload)  # payload snapshot present

	def test_push_failure_marks_record_failed(self):
		self._plan(["__TEST__TR2"])

		def fake_post(session, url, data, headers, timeout, verify):
			rows = json.loads(data)
			return FakeResp(200, json.dumps([{"BATCH": r["BATCH"], "ENTRYDATE": r["ENTRYDATE"],
											  "ENTRYTIME": r["ENTRYTIME"], "MESSAGE": "Batch not found"} for r in rows]))

		with patch.object(lp, "_csrf_token", return_value="TKN"), \
				patch.object(lp, "_http_post", side_effect=fake_post):
			lp.sweep()
		self.assertEqual(self._record("__TEST__TR2").status, "Failed")

	def _plan_dict(self):
		name = frappe.get_all("Loading Plan", filters={"truck_number": self.truck}, pluck="name")[0]
		return frappe._dict({"name": name, "truck_number": self.truck, "sap_plan_id": None})

	def test_trigger_push_refuses_sent(self):
		self._plan(["__TEST__TR3"])
		cn = self._coil_name("__TEST__TR3")
		frappe.db.set_value("Loading Plan Coil", cn, "sap_loading_status", "Sent")
		lp._sync_loading_record(frappe._dict({"name": cn, "coil_barcode": "__TEST__TR3"}), self._plan_dict(), "Sent")
		frappe.db.commit()
		rec = self._record("__TEST__TR3")
		with patch.object(frappe, "enqueue") as enq:
			res = lp.trigger_push(rec.name)
		self.assertEqual(res.get("skipped"), "already sent to SAP")
		enq.assert_not_called()

	def test_trigger_push_arms_failed(self):
		self._plan(["__TEST__TR4"])
		cn = self._coil_name("__TEST__TR4")
		frappe.db.set_value("Loading Plan Coil", cn, "sap_loading_status", "Failed")
		lp._sync_loading_record(frappe._dict({"name": cn, "coil_barcode": "__TEST__TR4"}), self._plan_dict(), "Failed")
		frappe.db.commit()
		rec = self._record("__TEST__TR4")
		with patch.object(frappe, "enqueue") as enq:
			res = lp.trigger_push(rec.name)
		self.assertTrue(res.get("ok"))
		self.assertEqual(self._statuses()["__TEST__TR4"], "Pending")
		enq.assert_called_once()

	# ---- auto-complete gating: T1 push is deferred until the plan completes ----
	def test_on_coil_loaded_defers_when_autocomplete_off(self):
		self._set_autocomplete(0)
		p = self._plan(["__TEST__DEF1"])
		with patch.object(frappe, "enqueue") as enq:
			lp.on_coil_loaded(p, p.coils[0])
		self.assertEqual(self._statuses()["__TEST__DEF1"], "Deferred")
		enq.assert_not_called()  # nothing pushed until the plan is completed

	def test_on_coil_loaded_arms_when_autocomplete_on(self):
		self._set_autocomplete(1)
		# coil not yet Loaded so the plan doesn't auto-complete on insert
		p = self._plan(["__TEST__ARM1"], coil_status="Pending", sap_status="Not Required")
		with patch.object(frappe, "enqueue") as enq:
			lp.on_coil_loaded(p, p.coils[0])
		self.assertEqual(self._statuses()["__TEST__ARM1"], "Pending")
		enq.assert_called_once()

	def test_on_coil_loaded_records_disabled_when_off(self):
		_settings(0)  # push feature off at load time
		p = self._plan(["__TEST__DIS1"], sap_status="Not Required")
		with patch.object(frappe, "enqueue") as enq:
			lp.on_coil_loaded(p, p.coils[0])
		# coil parked at Disabled, nothing pushed
		self.assertEqual(self._statuses()["__TEST__DIS1"], "Disabled")
		enq.assert_not_called()
		# but it IS Desk-visible: a tracking record was created with status Disabled
		rec = frappe.get_all("WMSLite SAP Loading",
							 filters={"coil_barcode": "__TEST__DIS1"}, fields=["status"])
		self.assertEqual(len(rec), 1)
		self.assertEqual(rec[0].status, "Disabled")

	def test_push_disabled_arms_and_sweeps_when_enabled(self):
		_settings(1)  # feature turned on now
		p = self._plan(["__TEST__DIS2"], sap_status="Disabled")
		with patch.object(frappe, "enqueue") as enq:
			res = lp.push_disabled(p.name)
		self.assertEqual(res.get("armed"), 1)
		self.assertEqual(self._statuses()["__TEST__DIS2"], "Pending")
		enq.assert_called_once()

	def test_push_disabled_noop_when_still_disabled(self):
		p = self._plan(["__TEST__DIS3"], sap_status="Disabled")
		_settings(0)
		res = lp.push_disabled(p.name)
		self.assertIn("disabled", res.get("skipped", ""))
		self.assertEqual(self._statuses()["__TEST__DIS3"], "Disabled")  # untouched

	def test_arm_deferred_releases_to_pending(self):
		self._set_autocomplete(0)
		p = self._plan(["__TEST__AD1", "__TEST__AD2"], sap_status="Deferred")
		with patch.object(frappe, "enqueue") as enq:
			res = lp.arm_deferred(p.name)
		self.assertEqual(res["armed"], 2)
		self.assertTrue(all(v == "Pending" for v in self._statuses().values()))
		enq.assert_called_once()

	def test_arm_deferred_noop_when_disabled(self):
		p = self._plan(["__TEST__AD3"], sap_status="Deferred")
		_settings(0)
		with patch.object(frappe, "enqueue") as enq:
			res = lp.arm_deferred(p.name)
		self.assertEqual(res.get("skipped"), "disabled")
		enq.assert_not_called()

	# ---- plan-status gating (LoadingPlan.recompute_counts) ----
	def test_full_load_holds_ready_to_complete_when_off(self):
		self._set_autocomplete(0)
		p = self._plan(["__TEST__RC1"])
		self.assertEqual(
			frappe.db.get_value("Loading Plan", p.name, "plan_status"), "Ready to Complete")

	def test_full_load_auto_completes_when_on(self):
		self._set_autocomplete(1)
		with patch("sbx_wmslite.outbound_sap.on_plan_completed"):
			p = self._plan(["__TEST__AC1"])
		self.assertEqual(
			frappe.db.get_value("Loading Plan", p.name, "plan_status"), "Completed")

	# ---- completing a plan releases its Deferred T1 pushes (single-sourced in
	#      LoadingPlan.on_update) ----
	def test_completion_arms_deferred(self):
		self._set_autocomplete(0)
		p = self._plan(["__TEST__CMP1"], sap_status="Deferred")
		self.assertEqual(
			frappe.db.get_value("Loading Plan", p.name, "plan_status"), "Ready to Complete")
		with patch("sbx_wmslite.outbound_sap.on_plan_completed") as gi, \
				patch.object(frappe, "enqueue") as enq:
			doc = frappe.get_doc("Loading Plan", p.name)
			doc.flags.operator_completing = True
			doc.save(ignore_permissions=True)
			frappe.db.commit()
		self.assertIn(doc.plan_status, ("Completed", "Completed (Short)"))
		gi.assert_called_once()  # SAP goods-issue fired
		self.assertEqual(self._statuses()["__TEST__CMP1"], "Pending")  # T1 armed
		enq.assert_called()  # sweep enqueued by arm_deferred
