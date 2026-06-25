"""Post-install setup for SBX WMSLite.

Creates the two app roles and grants their permissions. Runs after the app's
doctypes are migrated in, so the permission rows attach cleanly.
"""

import frappe


def after_install():
	import sbx_wmslite.setup_roles as roles

	roles.run()
	frappe.db.commit()
