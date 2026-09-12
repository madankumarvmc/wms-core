"""Add the RM Movement card + shortcuts to the Desk workspace.

v6_rm_movement force-reloaded the workspace on the strength of a comment saying
it "gains an RM Movement card" — but the workspace JSON was never actually
changed, so the reload imported the same four cards it already had. The JSON is
correct now; this patch is what makes an already-migrated site pick it up, since
v6 will not run again.

RM Movement adds no doctypes of its own — it carries its state on Bin Inventory
(`rm_*` columns) and its history on Coil Transaction (`RM Dispatch` / `RM Receipt`
rows). So the card points at those two, described from the RM angle. Anyone
looking for an "RM" doctype in Desk finds the card instead of nothing.

Why force=True: `bench migrate` imports a workspace JSON only when the DB has no
such Workspace yet, so on any deployed site the edited file is ignored unless the
reload is forced — the same reason v4_workspace exists.

The runtime-generated "Custom Reports" card (JIL Report and friends) is NOT
stored on the Workspace doc — Frappe builds it from Report records at render
time — so a forced reload cannot wipe it.
"""

import frappe


def execute():
	if frappe.db.exists("Workspace", "SBX WMSLite"):
		frappe.reload_doc("sbx_wmslite", "workspace", "sbx_wmslite", force=True)
		frappe.db.commit()
