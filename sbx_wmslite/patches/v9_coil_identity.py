"""Make the extracted batch code the coil identity, and seed the location master.

Until now the server stored whatever string it was handed (`qr_decode_rule =
Identity`) while the handheld extracted the batch code from it. The handheld was
right and the server was authoritative, which is the worst possible split: a
scanned coil became a SECOND Bin Inventory row keyed by its full 78-character QR,
sitting in a placeholder bin, while the real coil stayed in the yard under its
batch code. The receiving line then could not match its own stock.

This patch switches the rule on and repairs the rows written under the old one.

Order matters and is deliberate:
  1. safety check     — refuse to run if the feed's own rows look extractable
  2. location master  — the supplied list, plus every bin actually in use
  3. bogus bin_code   — repaired against the master, by longest matching prefix
  4. coil identities  — merge long-barcode rows onto their real short-code rows
  5. ledger rows      — rewrite in-app rows, keeping the original in coil_qr_raw
  6. settings         — switch the decode rule and turn validation on

Idempotent throughout: re-running finds nothing to do. It prints what it changed,
because a migration that silently touches production rows is not reviewable.
"""

import os

import frappe

from sbx_wmslite.decode import extract_inventory_batch

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
					   "fixtures", "bin_locations.txt")

# A descriptive coil QR always contains " - ". Real location labels never do --
# verified against all 843 bin codes and 66k feed rows in use at the time.
COIL_MARKER = " - "

# In-app rows are the only ones written under the broken identity. Feed rows carry
# whatever SAP calls the coil and are none of our business.
IN_APP = "In-app"


def execute():
	_assert_feed_is_not_extractable()
	_seed_locations()
	_repair_bogus_bin_codes()
	_merge_coil_identities()
	_rewrite_ledger_rows()
	_switch_on()
	frappe.db.commit()


def _assert_feed_is_not_extractable():
	"""Abort if a feed row looks like a descriptive QR.

	The whole migration rests on "in-app rows are wrong, feed rows are right". If
	the feed ever starts sending descriptive strings that assumption dies, and
	silently re-identifying 66k rows would be catastrophic. Better to stop.
	"""
	bad = frappe.db.count("Bin Inventory", {"source": ("!=", IN_APP),
											"coil_barcode": ("like", f"%{COIL_MARKER}%")})
	if bad:
		frappe.throw(
			f"v9_coil_identity: {bad} feed-sourced Bin Inventory rows contain "
			f"'{COIL_MARKER}'. The feed is no longer the clean side, so this "
			f"migration's core assumption does not hold. Investigate before re-running.")


def _repair_bogus_bin_codes():
	"""A coil QR scanned into a location field became a bin code with stock in it.

	These are a real location with a coil string glued onto the end, e.g.
	"JPR/CORR/CA1/E2/R2Jangalpur CR3 - GP Sheet-14959G - …" — the scanner fired
	twice into one field. The real location is recovered by finding the LONGEST
	master code the value starts with, which is deterministic, rather than by
	guessing where to cut the string.

	A value with no master prefix is left alone and reported. Guessing at a
	location is how stock ends up somewhere nobody can find it.
	"""
	rows = frappe.db.sql(
		"SELECT DISTINCT bin_code FROM `tabBin Inventory` WHERE bin_code LIKE %(m)s",
		{"m": f"%{COIL_MARKER}%"}, as_dict=True)
	if not rows:
		return
	master = frappe.get_all("WMSLite Bin", pluck="name")
	for r in rows:
		candidates = [c for c in master if r.bin_code.startswith(c)]
		if not candidates:
			print(f"  bin_code NOT repaired (no known location is a prefix): {r.bin_code[:60]}…")
			continue
		fixed = max(candidates, key=len)
		n = frappe.db.count("Bin Inventory", {"bin_code": r.bin_code})
		frappe.db.sql("UPDATE `tabBin Inventory` SET bin_code = %(new)s WHERE bin_code = %(old)s",
					  {"new": fixed, "old": r.bin_code})
		print(f"  bin_code repaired: {r.bin_code[:45]}… -> {fixed}  ({n} coils)")


def _seed_locations():
	"""The supplied master, plus every bin actually in use."""
	if not os.path.exists(FIXTURE):
		frappe.throw(f"v9_coil_identity: location fixture missing at {FIXTURE}")

	master, dropped = [], []
	for line in open(FIXTURE, encoding="utf-8"):
		code = line.strip()
		if not code or code.startswith("#"):
			continue
		# Defensive: the fixture is already clean, but re-apply the rule so a future
		# re-import of a dirty export cannot quietly seed coil strings as locations.
		if COIL_MARKER in code:
			dropped.append(code)
			continue
		master.append(code)

	observed = [r.bin_code for r in frappe.db.sql(
		"SELECT DISTINCT bin_code FROM `tabBin Inventory` WHERE bin_code IS NOT NULL "
		"AND bin_code != ''", as_dict=True) if COIL_MARKER not in r.bin_code]

	created = 0
	for code, source in [(c, "Master") for c in master] + [(c, "Observed") for c in observed]:
		if frappe.db.exists("WMSLite Bin", code):
			continue
		doc = frappe.new_doc("WMSLite Bin")
		doc.bin_code = code
		doc.source = source
		doc.insert(ignore_permissions=True)
		created += 1

	print(f"  locations: {created} created "
		  f"({len(master)} from the master list, {len(observed)} observed in stock)")
	if dropped:
		print(f"  locations: {len(dropped)} coil strings dropped from the fixture")


def _merge_coil_identities():
	"""Move in-app long-barcode rows onto the coil they were always meant to be."""
	rows = frappe.db.sql(
		"SELECT name FROM `tabBin Inventory` WHERE source = %(s)s AND coil_barcode LIKE %(m)s",
		{"s": IN_APP, "m": f"%{COIL_MARKER}%"}, as_dict=True)
	for r in rows:
		long_code = r.name
		short = extract_inventory_batch(long_code)
		if not short or short == long_code:
			print(f"  SKIPPED (no batch code extractable): {long_code[:55]}…")
			continue

		ghost = frappe.get_doc("Bin Inventory", long_code)
		if frappe.db.exists("Bin Inventory", short):
			# The real coil already exists — carry the RM state across to it. Its own
			# bin_code is left alone: the feed knows where the coil physically is, and
			# the ghost only ever held a placeholder.
			carry = {f: ghost.get(f) for f in (
				"rm_status", "rm_destination", "rm_source_bin", "rm_cycle",
				"rm_dispatched_by", "rm_dispatched_at", "rm_received_by",
				"rm_received_at", "rm_unplanned", "rm_expected_destination")
				if ghost.get(f) not in (None, "", 0)}
			carry["coil_qr_raw"] = long_code
			if carry:
				frappe.db.set_value("Bin Inventory", short, carry, update_modified=False)
			action = "merged onto existing"
		else:
			doc = frappe.new_doc("Bin Inventory")
			for f in ("bin_code", "status", "qc", "material_grade", "weight", "plant",
					  "product", "sized", "zone", "aisle", "source", "scanned_at",
					  "rm_status", "rm_destination", "rm_source_bin", "rm_cycle",
					  "rm_dispatched_by", "rm_dispatched_at", "rm_received_by",
					  "rm_received_at", "rm_unplanned", "rm_expected_destination"):
				doc.set(f, ghost.get(f))
			doc.coil_barcode = short
			doc.coil_qr_raw = long_code
			doc.insert(ignore_permissions=True)
			action = "renamed to"

		# Repoint the ledger before the ghost disappears from under its link field.
		frappe.db.sql("UPDATE `tabCoil Transaction` SET coil_barcode = %(new)s, "
					  "bin_inventory = %(new)s, coil_qr_raw = COALESCE(NULLIF(coil_qr_raw, ''), %(old)s) "
					  "WHERE coil_barcode = %(old)s", {"new": short, "old": long_code})
		frappe.delete_doc("Bin Inventory", long_code, force=1, ignore_permissions=True)
		print(f"  coil {action} {short}  (was {long_code[:45]}…)")


def _rewrite_ledger_rows():
	"""In-app ledger rows whose coil never had a Bin Inventory row to merge."""
	rows = frappe.db.sql(
		"SELECT name, coil_barcode FROM `tabCoil Transaction` "
		"WHERE source = %(s)s AND coil_barcode LIKE %(m)s",
		{"s": IN_APP, "m": f"%{COIL_MARKER}%"}, as_dict=True)
	for r in rows:
		short = extract_inventory_batch(r.coil_barcode)
		if not short or short == r.coil_barcode:
			continue
		frappe.db.set_value("Coil Transaction", r.name,
							{"coil_barcode": short, "coil_qr_raw": r.coil_barcode},
							update_modified=False)
	if rows:
		print(f"  ledger rows rewritten to the extracted code: {len(rows)}")


def _switch_on():
	settings = frappe.get_single("WMSLite Settings")
	changed = []
	if settings.qr_decode_rule != "Inventory Barcode":
		settings.qr_decode_rule = "Inventory Barcode"
		changed.append("qr_decode_rule = Inventory Barcode")
	if not settings.bin_validation_enabled:
		settings.bin_validation_enabled = 1
		changed.append("bin_validation_enabled = 1")
	if changed:
		settings.save(ignore_permissions=True)
		print("  settings: " + "; ".join(changed))
