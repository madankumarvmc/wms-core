"""RM Movement: create the two roles, seed its settings, index the new columns.

No data backfill. Existing coils keep a blank `rm_status`, which is the correct
value — they have never moved to a line. (This is also why `rm_status` ships with
no `default` in the DocType JSON: a default would rewrite every one of the ~50k
rows on migrate and assert a state none of them were ever in.)
"""

import frappe

import sbx_wmslite.setup_roles as setup_roles

# A Single's fields read NULL until the doc is saved once, so a JSON `default` is
# not enough — the same trap v2_loading_model exists to fix.
DEFAULTS = {
	"rm_received_history_hours": 12,
	"rm_unknown_bin": "UNKNOWN",
}


def execute():
	# Creates 'RM Gate Operator' / 'RM Line Operator' and grants their perms.
	# Idempotent, and safe to re-run for the pre-existing roles.
	setup_roles.run()

	settings = frappe.get_single("WMSLite Settings")
	changed = False
	for field, value in DEFAULTS.items():
		if not settings.get(field):
			settings.set(field, value)
			changed = True
	if changed:
		settings.save(ignore_permissions=True)

	# rm_status / rm_destination get their own indexes from `search_index: 1` in the
	# DocType JSON. This composite serves the query on the hot path — the receiving
	# line's expected list, `rm_destination = X AND rm_status = 'In Transit to Line'`
	# — over a table that is already ~50k rows. Neither single-column index can
	# satisfy both predicates, and rm_status has terrible selectivity on its own
	# (almost every row is blank), so the optimiser would scan.
	# Leading column is rm_destination because it is the selective one.
	if frappe.db.exists("DocType", "Bin Inventory"):
		frappe.db.add_index("Bin Inventory", ["rm_destination", "rm_status"])

	# The ledger reconciliation in rm_open_line filters
	# `transaction_type = 'RM Dispatch' AND rm_destination = X AND reversed = 0`
	# over ~314k rows. transaction_type had no index at all before this.
	if frappe.db.exists("DocType", "Coil Transaction"):
		frappe.db.add_index("Coil Transaction", ["transaction_type", "rm_destination"])

	# The workspace JSON gains an RM Movement card. bench migrate imports a
	# workspace only when the DB has none, so the reload has to be forced — see
	# v4_workspace, which exists for exactly this reason.
	frappe.reload_doc("sbx_wmslite", "workspace", "sbx_wmslite", force=True)

	frappe.db.commit()
