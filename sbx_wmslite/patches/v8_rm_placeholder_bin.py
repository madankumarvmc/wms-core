"""Re-stamp RM placeholder bins that were created with a test value.

`rm_api._apply_rm` invents a Bin Inventory row when a coil is scanned at the gate
that nothing has ever seen before, using `WMSLite Settings.rm_unknown_bin` as the
mandatory bin_code. For a period that setting held "__TEST__RM_UNKNOWN" — a value
the test suite wrote and never restored — so live UAT scans stamped a __TEST__
bin code onto real production coils.

Harmless but wrong: those coils show up in the console's Inventory view sitting in
a bin named __TEST__RM_UNKNOWN. This renames them to the intended placeholder.

Safe by construction:
  * only rows whose bin_code is exactly the old test value are touched;
  * `scanned_at` on these rows is NULL, so the first real SAP feed or Inventory
    Count scan overwrites the placeholder anyway (upsert_bin's staleness guard
    only fires when a timestamp is already stored) — this just stops it looking
    like test data in the meantime;
  * RM state (rm_status / rm_destination / rm_cycle) is NOT touched, so nothing
    in transit moves.

Idempotent: re-running finds no rows.
"""

import frappe

OLD = "__TEST__RM_UNKNOWN"


def execute():
	settings_value = frappe.db.get_single_value("WMSLite Settings", "rm_unknown_bin")
	# Never rename onto the very value we are replacing.
	new = settings_value if settings_value and settings_value != OLD else "UNKNOWN"

	rows = frappe.get_all("Bin Inventory", filters={"bin_code": OLD}, pluck="name")
	for coil in rows:
		frappe.db.set_value("Bin Inventory", coil, "bin_code", new, update_modified=False)

	if rows:
		print(f"v8_rm_placeholder_bin: re-stamped {len(rows)} placeholder bin(s) -> {new}")
	frappe.db.commit()
