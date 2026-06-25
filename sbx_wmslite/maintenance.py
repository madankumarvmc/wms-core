"""Operational helpers for SBX WMSLite.

purge_test_data() removes every row whose truck number is __TEST__-prefixed,
across Loading Plans, Coil Load Events, and SAP Logs. Run:

    bench --site <site> execute sbx_wmslite.maintenance.purge_test_data
"""

import frappe

PREFIX = "__TEST__"


def purge_test_data():
	n = 0
	for name in frappe.get_all("Loading Plan",
			filters={"truck_number": ["like", PREFIX + "%"]}, pluck="name"):
		frappe.delete_doc("Loading Plan", name, force=1, ignore_permissions=True)
		n += 1
	for name in frappe.get_all("Coil Load Event",
			filters={"truck_number": ["like", PREFIX + "%"]}, pluck="name"):
		frappe.delete_doc("Coil Load Event", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("WMSLite SAP Log",
			filters={"truck_number": ["like", PREFIX + "%"]}, pluck="name"):
		frappe.delete_doc("WMSLite SAP Log", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("WMSLite SAP Confirmation",
			filters={"truck_number": ["like", PREFIX + "%"]}, pluck="name"):
		frappe.delete_doc("WMSLite SAP Confirmation", name, force=1, ignore_permissions=True)
	frappe.db.commit()
	print(f"Purged {n} test loading plan(s) and related events/logs/confirmations.")
