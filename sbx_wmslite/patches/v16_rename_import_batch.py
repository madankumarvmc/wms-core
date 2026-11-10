"""Move WMS Lite's upload batches off the shared "Import Batch" doctype name.

Inventory Recording and Yard Management also define a doctype called "Import
Batch". A doctype name is site-wide, so on a site with two of these apps the
table belonged to whichever migrated last — and Inventory Recording's node hooks
(keyed by that name) fired on WMS uploads: "Select a node first." on every
console import. WMS Lite now uses "WMSLite Import Batch" (synced by migrate
before this post_model_sync patch runs).

Two steps, both idempotent:

1. Copy WMS Lite's rows across, keeping their names so Loading Plan.import_batch
   (plain text holding the batch name) still resolves. WMS rows are picked by
   their dataset, or by a Loading Plan pointing at them. The originals are left
   in the shared table: it belongs to another app here, and its rows are not ours
   to delete.

2. Hand the old "Import Batch" registration back. Where it is still registered
   to WMS Lite, its controller is gone now, and migrate's orphan sweep (which runs
   after patches) would delete the registration — out from under the other app
   that ships it. So if another installed app defines import_batch, reload that
   app's definition, which makes it the owner. If none does, the sweep removes the
   registration; the table and its rows stay, as Frappe never drops it.
"""

import os

import frappe

OLD, NEW = "Import Batch", "WMSLite Import Batch"
WMS_DATASETS = ("Loading Plan", "Bin Inventory", "Bin Master")
COLUMNS = ("name", "creation", "modified", "modified_by", "owner", "docstatus", "idx",
		   "batch_label", "dataset", "target_doctype", "status", "file_name", "file_url",
		   "import_time", "imported_by", "total_rows", "inserted", "errors", "error_report")


def execute():
	copied = _copy_rows()
	owner = _hand_back_old_doctype()
	# The workspace's "Import Batch" link now points at the new doctype; a saved
	# Workspace is not re-synced by migrate unless forced (see v4/v7/v12).
	if frappe.db.exists("Workspace", "SBX WMSLite"):
		frappe.reload_doc("sbx_wmslite", "workspace", "sbx_wmslite", force=True)
	frappe.db.commit()
	print(f"  WMSLite Import Batch: {copied} batch(es) copied; old '{OLD}' now owned by: {owner}")


def _copy_rows():
	if not frappe.db.table_exists(OLD):
		return 0
	have = set(frappe.db.get_table_columns(OLD))
	cols = [c for c in COLUMNS if c in have]
	where = ["name IN (SELECT import_batch FROM `tabLoading Plan` WHERE COALESCE(import_batch, '') <> '')"]
	if "dataset" in have:
		where.append("dataset IN %(ds)s")
	col_sql = ", ".join(f"`{c}`" for c in cols)
	before = frappe.db.count(NEW)
	frappe.db.sql(f"""
		INSERT IGNORE INTO `tab{NEW}` ({col_sql})
		SELECT {col_sql} FROM `tab{OLD}` WHERE {" OR ".join(where)}""", {"ds": WMS_DATASETS})
	return frappe.db.count(NEW) - before


def _hand_back_old_doctype():
	module = frappe.db.get_value("DocType", OLD, "module")
	if module is None:
		return "nobody (not registered)"
	if module != "SBX WMSLite":
		return module   # already another app's — nothing to do
	for app in frappe.get_installed_apps():
		if app == "sbx_wmslite":
			continue
		for mod in frappe.get_module_list(app):
			path = os.path.join(frappe.get_module_path(mod), "doctype", "import_batch", "import_batch.json")
			if os.path.exists(path):
				frappe.reload_doc(frappe.scrub(mod), "doctype", "import_batch", force=True)
				return frappe.db.get_value("DocType", OLD, "module")
	return "nobody (orphan sweep will remove the registration; table kept)"
