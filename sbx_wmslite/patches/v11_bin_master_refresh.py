"""Re-seed the location master from the fixture after a plant update.

`v9_coil_identity` seeded the master once, from the list the plant supplied then.
New bays get built, so the fixture grows and the master has to catch up — and
until it does, `bin_validation_enabled` refuses every scan of the new bay, which
is the floor-visible symptom this patch exists to clear.

This runs the additive half of v9's seeding and nothing else. It does not remove
codes that have dropped out of the fixture: `WMSLite Bin.name == bin_code` and
live inventory and ledger rows point at those names, so retiring a location is a
deliberate act (tick `disabled`), never a side effect of a migration.

Safe to re-register for a future refresh — re-running finds nothing to do.
"""

import frappe

from sbx_wmslite import locations


def execute():
	r = locations.seed_from_fixture()
	print(f"  locations: {r['created']} created ({r['in_fixture']} in the fixture)")
	if r["dropped"]:
		print(f"  locations: {r['dropped']} coil strings dropped from the fixture")
	frappe.db.commit()
