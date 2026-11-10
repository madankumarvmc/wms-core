"""Handheld Inventory lookup card: settings defaults + the paging index.

Both Settings fields are new; a Single's default only applies when the doc is
saved through the form, so on an existing site they read NULL (same trap as
v12-v14). The lookup lists Bin Inventory ORDER BY bin_code, coil_barcode with
keyset paging, so a composite index keeps every "Show more" an index range scan
instead of a filesort of the 9.7k-coil bins.
"""

import frappe


def execute():
	# get_single_value casts a missing Check to 0, so "is None" never fires: look for
	# the Singles row itself. The card is ON by default (read-only, every role).
	if not frappe.db.exists("Singles", {"doctype": "WMSLite Settings", "field": "inventory_view_enabled"}):
		frappe.db.set_single_value("WMSLite Settings", "inventory_view_enabled", 1)
	if not frappe.db.get_single_value("WMSLite Settings", "inventory_page_size"):
		frappe.db.set_single_value("WMSLite Settings", "inventory_page_size", 20)
	if frappe.db.exists("DocType", "Bin Inventory"):
		frappe.db.add_index("Bin Inventory", ["bin_code", "coil_barcode"])
	frappe.db.commit()
