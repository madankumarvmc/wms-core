"""Seed Loading Model config defaults on installs that predate the fields.

A Single's new Select fields read as NULL until first saved, so set the shipped
defaults explicitly. Idempotent — only fills when unset.
"""

import frappe


def execute():
	if not frappe.db.get_single_value("WMSLite Settings", "loading_group_mode"):
		frappe.db.set_single_value("WMSLite Settings", "loading_group_mode", "By Shipment")
	if not frappe.db.get_single_value("WMSLite Settings", "completion_mode"):
		frappe.db.set_single_value("WMSLite Settings", "completion_mode", "Whole Truck")
	frappe.db.commit()
