"""Index bin_code on Bin Inventory and Coil Transaction for the console views.

Bin Inventory: `console_api.get_bin_rollup` does GROUP BY bin_code over the whole
table (53k+ rows here) and the coil drill-down filters on it.

Coil Transaction: the transactions search is
`coil_barcode LIKE 'q%' OR bin_code LIKE 'q%'`. coil_barcode was already indexed,
but an OR with an UNindexed column throws the whole plan away — measured
EXPLAIN went type=ALL over 254k rows despite coil_barcode being a candidate key.
Indexing bin_code lets the optimiser index_merge the two instead.

Idempotent: add_index is a no-op when the index already exists.
"""

import frappe


def execute():
	for doctype in ("Bin Inventory", "Coil Transaction"):
		if frappe.db.exists("DocType", doctype):
			frappe.db.add_index(doctype, ["bin_code"])
	# `source` splits the two scan populations and is the transactions view's
	# primary filter (SAP Feed vs Inventory Count). Unindexed it cost 1785ms to
	# count one source over 260k rows.
	if frappe.db.exists("DocType", "Coil Transaction"):
		frappe.db.add_index("Coil Transaction", ["source"])
	frappe.db.commit()
