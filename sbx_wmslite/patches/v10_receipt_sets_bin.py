"""Backfill bin_code for coils that were already received before receipts set it.

`rm_receive` now records the coil's location the way an Inventory Count scan does
(see the rm_api docstring for why). Coils received BEFORE that change still carry
whatever bin_code they had at the time — for a coil the SAP feed has never sent,
that is the "UNKNOWN" placeholder rm_api had to invent, and nothing else would
ever fill it in.

Only coils whose destination is a known location are backfilled. A destination
that is not in the WMSLite Bin master (the LOCATION1/2/3 codes from early smoke
testing, dispatched before validation existed) is left alone and reported: writing
it would put a bin_code back into Bin Inventory that is not a real place, which is
the exact class of junk v9 just cleaned out.

Idempotent: re-running finds nothing, since the rows it fixes no longer match.
"""

import frappe

RECEIVED = "Received"


def execute():
	rows = frappe.get_all(
		"Bin Inventory",
		filters={"rm_status": RECEIVED},
		fields=["name", "bin_code", "rm_destination", "rm_received_at"],
		limit_page_length=0)

	fixed, skipped = 0, []
	for r in rows:
		dest = (r.rm_destination or "").strip()
		if not dest or r.bin_code == dest:
			continue
		if not frappe.db.exists("WMSLite Bin", dest):
			skipped.append((r.name, dest))
			continue
		frappe.db.set_value("Bin Inventory", r.name, {
			"bin_code": dest,
			# Stamp the receipt time so a later genuine feed push still wins on
			# upsert_bin's latest-scan-wins comparison.
			"scanned_at": r.rm_received_at,
		}, update_modified=False)
		print(f"  {r.name}: {r.bin_code} -> {dest}")
		fixed += 1

	print(f"v10_receipt_sets_bin: {fixed} received coil(s) given their line as bin_code")
	for name, dest in skipped:
		print(f"  SKIPPED {name}: destination {dest!r} is not in the location master")
	frappe.db.commit()
