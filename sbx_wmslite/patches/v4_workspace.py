"""Refresh the SBX WMSLite Desk workspace: grouped cards + filtered shortcuts.

The workspace is regrouped into four cards (Loading / Inventory Count /
SAP Integration / Setup) and gains the previously-missing 'WMSLite SAP Loading'
doctype link.

Why force=True: `bench migrate` imports the workspace JSON only when the DB has
no such Workspace yet. On an existing install (which every deployed site is) the
edited JSON is ignored, so the reload has to be forced or nobody sees the change.
Idempotent — re-running just re-imports the same file.
"""

import frappe


def execute():
	if frappe.db.exists("Workspace", "SBX WMSLite"):
		frappe.reload_doc("sbx_wmslite", "workspace", "sbx_wmslite", force=True)
		frappe.db.commit()
