"""Resolving a scanned location — decode, then check it is somewhere real.

Every flow that asks an operator to scan a location goes through `resolve_location`.
Before it existed each flow called `decode_bin_code` and trusted the result, which
meant any string at all became a location:

  * a coil QR scanned into "Scan the destination line" became a destination, and
    one such string is still a `bin_code` in Bin Inventory with two real coils in it;
  * `LOCATION1` / `LOCATION2` / `LOCATION3` are in the RM data for the same reason;
  * a mistyped bin silently opened an empty count instead of saying "no such bin".

The master is `WMSLite Bin`. Validation is behind `bin_validation_enabled` so a
missing label can be worked around without a deploy — a master that refuses a
location the floor genuinely needs is worse than no master at all.
"""

import frappe
from frappe import _
from frappe.utils import cint

from sbx_wmslite.decode import decode_bin_code

# Refusals carry the terminal-failure sentinel so a queued offline event that can
# never succeed is offered as dismissable rather than retried forever.
from sbx_wmslite.tasks import throw_final


def validation_on():
	"""Read fresh, not via the cached Single — an admin may have just changed it."""
	return bool(cint(frappe.db.get_single_value("WMSLite Settings", "bin_validation_enabled")))


def is_known(bin_code):
	"""Is this a location we know about and still allow scanning?"""
	if not bin_code:
		return False
	row = frappe.db.get_value("WMSLite Bin", bin_code, ["disabled"], as_dict=True)
	return bool(row) and not cint(row.disabled)


def resolve_location(raw, label, require_line=False):
	"""Decode a scanned location and assert it is real. Returns the bin code.

	`label` names the thing being scanned ("destination", "line", "bin") so the
	refusal reads like the screen the operator is looking at.

	`require_line` additionally demands `is_line` — reserved for RM destinations
	once lines are marked up in the master. It is NOT enforced while no bin carries
	the flag, because that would refuse every destination on day one.
	"""
	code = decode_bin_code(raw)
	if not code:
		throw_final(_("Couldn't read the {0} barcode").format(label))

	if not validation_on():
		return code

	if not is_known(code):
		# Say what was scanned. A coil QR pasted into a location field is the common
		# case and is obvious the moment the operator sees it echoed back.
		throw_final(
			_("{0} is not a known location — check you scanned the {1} barcode")
			.format(code[:60], label))

	if require_line and _any_line_marked() and not cint(
			frappe.db.get_value("WMSLite Bin", code, "is_line")):
		throw_final(_("{0} is not a production line").format(code))

	return code


def _any_line_marked():
	"""True once somebody has actually designated lines in the master."""
	return bool(frappe.db.count("WMSLite Bin", {"is_line": 1}))
