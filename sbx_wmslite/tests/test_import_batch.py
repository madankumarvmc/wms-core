"""Console uploads record their batch in "WMSLite Import Batch".

The doctype was "Import Batch" until 2026-11-04, a name Inventory Recording and
Yard Management also define; on a shared site another app's hooks fired on WMS
uploads ("Select a node first."). What is pinned: a commit creates the WMS-owned
batch, the Bin Master import runs through it end to end, and rollback marks it.

The file read is stubbed (_mapped_rows) and the job runs inline (enqueue
patched), so no upload or worker is involved. __TEST__IB data only.

    bench --site <site> run-tests --app sbx_wmslite --module sbx_wmslite.tests.test_import_batch
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from sbx_wmslite import console_api

P = "__TEST__IB"
NEW_CODES = [f"{P}/A1", f"{P}/A2"]


def _cleanup():
	frappe.set_user("Administrator")
	for n in frappe.get_all("WMSLite Bin", filters={"bin_code": ["like", f"{P}%"]}, pluck="name"):
		frappe.delete_doc("WMSLite Bin", n, force=1, ignore_permissions=True)
	for n in frappe.get_all("WMSLite Import Batch", filters={"batch_label": ["like", f"{P}%"]}, pluck="name"):
		frappe.delete_doc("WMSLite Import Batch", n, force=1, ignore_permissions=True)
	frappe.db.commit()


class TestWmsImportBatch(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_cleanup()

	@classmethod
	def tearDownClass(cls):
		_cleanup()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")

	def _commit(self, rows, label):
		"""import_commit with the background job run inline on stubbed rows."""
		def run_now(method, **kw):
			frappe.get_attr(method)(**{k: v for k, v in kw.items()
									   if k in ("batch", "file_url", "mapping", "dataset")})
		with patch.object(frappe, "enqueue", side_effect=run_now), \
				patch.object(console_api, "_mapped_rows", return_value=rows):
			return console_api.import_commit("/private/files/__test__.xlsx", {"bin_code": "Bin Code"},
											 label=label, dataset="Bin Master")["batch"]

	def test_the_doctype_is_wms_owned(self):
		self.assertEqual(frappe.db.get_value("DocType", "WMSLite Import Batch", "module"), "SBX WMSLite")

	def test_bin_master_commit_runs_through_the_wms_batch(self):
		rows = [{"bin_code": c, "zone": "Z1"} for c in NEW_CODES] + [{"bin_code": ""}]
		batch = self._commit(rows, f"{P} bin master")
		self.assertTrue(batch.startswith("WIMP-"), batch)
		b = frappe.get_doc("WMSLite Import Batch", batch)
		self.assertEqual((b.dataset, b.target_doctype), ("Bin Master", "WMSLite Bin"))
		self.assertEqual((b.status, b.total_rows, b.errors), ("Partial", 3, 1))   # the blank code is rejected
		for c in NEW_CODES:
			self.assertEqual(frappe.db.get_value("WMSLite Bin", c, ["zone", "source"]), ("Z1", "Upload"))

	def test_rollback_reads_the_wms_batch(self):
		# A Bin Master upload refuses rollback by design; the refusal proves the
		# dataset was read from the WMS batch, not another app's "Import Batch".
		batch = self._commit([{"bin_code": f"{P}/R1"}], f"{P} rollback")
		with self.assertRaises(frappe.ValidationError):
			console_api.delete_import_batch(batch)
		self.assertEqual(frappe.db.get_value("WMSLite Import Batch", batch, "status"), "Completed")
