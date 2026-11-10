"""RM line receipt -> SAP T2 push: settings default + workspace shortcut. No backfill.

The Coil Transaction sap_* fields already exist (v12); WMSLite SAP Log gains the
"RM Receipt" action and WMSLite Settings the rm_push_enabled / rm_txn_type
fields through the doctype sync that runs before post_model_sync patches.

Deliberately NO sap_status backfill: every receipt made before this shipped
stays NULL and is never pushed. Only receipts made after this release go to
SAP; historic ones can be armed by hand if the client wants them (they would go
out with their original ENTRYDATE/ENTRYTIME).
"""

import frappe


def execute():
	# A Single's field default only applies when the doc is saved through the
	# form; a freshly added field reads NULL on an existing site (same trap as
	# v2/v6/v12). build_rm_row falls back to "T2" regardless — this just makes the
	# Settings page show the value it is actually using.
	if not frappe.db.get_single_value("WMSLite Settings", "rm_txn_type"):
		frappe.db.set_single_value("WMSLite Settings", "rm_txn_type", "T2")

	# The workspace JSON changed ("SAP Count Pending" -> "SAP Scans Pending", since
	# the Pending filter now also catches RM receipts); a saved Workspace is not
	# re-synced by migrate unless forced (see v4/v7/v12).
	if frappe.db.exists("Workspace", "SBX WMSLite"):
		frappe.reload_doc("sbx_wmslite", "workspace", "sbx_wmslite", force=True)
	frappe.db.commit()
