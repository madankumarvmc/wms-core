"""Inventory Count rollout — for installs that predate the feature.

`after_install` seeds roles on a fresh install; existing sites need this patch to
gain the new `Inventory Recorder` role + its permissions and the count settings
defaults. Idempotent (setup_roles guards with exists()), safe to re-run.
"""

import frappe


def execute():
	import sbx_wmslite.setup_roles as roles

	roles.run()

	# Seed the count-settings defaults if never set (the Single may predate the
	# fields, so a JSON `default` won't have been applied to the stored doc).
	if not frappe.db.get_single_value("WMSLite Settings", "count_qc_default"):
		frappe.db.set_single_value("WMSLite Settings", "count_qc_default", "OK")
	frappe.db.commit()
