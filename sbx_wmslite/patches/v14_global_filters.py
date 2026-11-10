"""Console global filters (Site / Material Type): seed the site master, default the
pipe keywords, classify the existing scan ledger, index the new columns.

The `WMSLite Site` / `WMSLite Site Prefix` doctypes and the Coil Transaction
`site` / `material_type` columns (with their single-column indexes, via
search_index) are synced by `bench migrate` before post_model_sync patches run.

The backfill touches every Coil Transaction row (404k on the client's box) in
month-sized chunks with a commit each, so no single statement holds a long lock;
still, run the migrate off-shift.
"""

import frappe

from sbx_wmslite import classify


def execute():
	classify.seed_sites()
	# A Single's field default only applies when the doc is saved through the
	# form; a freshly added field reads NULL on an existing site (same trap as
	# v12/v13). material_for_product falls back to the default regardless — this
	# just makes the Settings page show the value it is actually using.
	if not frappe.db.get_single_value("WMSLite Settings", "material_pipe_keywords"):
		frappe.db.set_single_value("WMSLite Settings", "material_pipe_keywords",
								   classify.DEFAULT_PIPE_KEYWORDS)
	frappe.db.commit()
	classify.clear_cache()
	n = classify.backfill_transactions()
	print(f"  classified {n} Coil Transaction rows")
	# The dashboard's scan tiles filter on site over a scanned_at range.
	if frappe.db.exists("DocType", "Coil Transaction"):
		frappe.db.add_index("Coil Transaction", ["site", "scanned_at"])
	frappe.db.commit()
