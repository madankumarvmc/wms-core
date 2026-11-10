"""Inventory Count -> SAP T1 push: settings default + workspace shortcut. No backfill.

The Coil Transaction (sap_* fields, sap_status index) and WMSLite SAP Log
("Inventory Count" action) doctypes are synced by `bench migrate` before
post_model_sync patches run, so their columns already exist here.

Deliberately NO sap_status backfill: every historic row — SAP feed, uploads, RM
Movement and count scans made before this shipped — stays NULL and is never
pushed. Only scans made after this release go to SAP.
"""

import frappe


def execute():
	# A Single's field default only applies when the doc is saved through the
	# form; a freshly added field reads NULL on an existing site (same trap as
	# v2/v6). build_count_row falls back to "T1" regardless — this just makes the
	# Settings page show the value it is actually using.
	if not frappe.db.get_single_value("WMSLite Settings", "count_txn_type"):
		frappe.db.set_single_value("WMSLite Settings", "count_txn_type", "T1")

	# The workspace JSON changed (new "SAP Count Pending" shortcut + link); a
	# saved Workspace is not re-synced by migrate unless forced (see v4/v7).
	if frappe.db.exists("Workspace", "SBX WMSLite"):
		frappe.reload_doc("sbx_wmslite", "workspace", "sbx_wmslite", force=True)
	frappe.db.commit()
