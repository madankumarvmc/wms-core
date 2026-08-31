"""Create the 'WMS Manager' role + its console permissions on existing installs.

Idempotent (setup_roles guards with exists()), safe to re-run. No user is granted
the role here — assign it in the Desk to whoever should onboard WMS users.
"""

import frappe


def execute():
	import sbx_wmslite.setup_roles as roles

	roles.run()
	frappe.db.commit()
