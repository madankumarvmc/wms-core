"""Post-install setup for SBX WMSLite.

Creates the two app roles and grants their permissions, and seeds the location
master. Runs after the app's doctypes are migrated in, so the permission rows
attach cleanly.

The location seed was previously only done by a patch, which meant a fresh
install came up with an empty `WMSLite Bin` table — harmless only because
`bin_validation_enabled` defaults off. The moment it is switched on, every scan
on that site is refused. Seeding here closes that gap.
"""

import frappe


def after_install():
	import sbx_wmslite.setup_roles as roles
	from sbx_wmslite import locations

	roles.run()
	r = locations.seed_from_fixture()
	print(f"  locations: {r['created']} seeded from the fixture")
	# Site master for the console's global filters (patch v14 does the same on
	# existing sites; patches never run on a fresh install).
	from sbx_wmslite import classify

	r = classify.seed_sites()
	print(f"  sites: {r['created']} seeded")
	frappe.db.commit()
