"""Coil QR decoding — the one place a scan becomes an identity.

SAP sends a coil barcode (the match key). The physical coil QR may encode extra
data (GS1 / embedded fields); we extract the coil ID from it before matching.

This is intentionally a thin, configurable seam — the real GS1-128 / custom
format drops in here without touching any flow logic. Rule is read from
WMSLite Settings:

  * Identity          — the scanned QR string IS the coil ID (trimmed).
  * Regex Extract     — capture group 1 of `qr_decode_regex` is the coil ID.
  * Inventory Barcode — the plant's descriptive QR; the batch code is extracted
                        from it by `extract_inventory_batch` below.

WHY THE SERVER MUST DECODE
--------------------------
It briefly did not. The handheld carried its own copy of the Inventory Barcode
rule while the server ran Identity, so a scan of

    "Jangalpur CR3 - H.R.COIL-JJG40788 - 2.500 mm  X 1230 - 26.420 MT- H025001230C2"

meant "JJG40788" to the handheld and the whole 78-char string to the server. The
server won, because the server stores. That produced a SECOND Bin Inventory row
for a coil that already existed as "JJG40788", and made the line's expected list
unmatchable against the operator's own scan.

Decoding here means every caller gets the same answer — the PWA, the SAP feed,
the console importer, and anything added later. The handheld keeps a copy of the
rule for offline matching only; `tests/test_decode_parity.py` asserts the two
implementations agree, and is the thing that stops them drifting apart again.
"""

import re

import frappe

# Segment-3 prefixes meaning "this is a product/extra code, so the batch is in
# segment 4 instead". Each entry is (slice_length, expected) and is compared as
# `segment[:slice_length] == expected`, mirroring the handheld's JS character for
# character rather than reading as idiomatic Python.
#
# That is deliberate. `(3, "RM")` can never match — a 3-character slice is never
# equal to a 2-character string — so the RM branch is dead, exactly as it is dead
# in the JS. Writing it as `startswith("RM")` would be the obvious "fix" and would
# silently re-identify every coil whose segment-3 starts with RM, splitting it
# from the rows already stored under the old identity. The two implementations
# have to agree more than either has to be tidy; correcting this is a deliberate
# data migration, not a cleanup, and would have to change both sides at once.
_EXTRA_TESTS = (
	(3, "GL "), (3, "P1B"), (2, "P2"), (3, "RM"), (3, "API"), (6, "APISFG"),
	(3, "PGL"), (3, "OEM"), (6, "EXPORT"), (3, "GC "), (4, "PGI "),
)


def extract_inventory_batch(raw: str) -> str:
	"""The batch code inside a descriptive inventory QR.

	Split on "-", trim, then:
	  1. >=5 segments and segment-2 ends "OEM"        -> "seg3-seg4"
	  2. segment-3 is a product/extra code            -> seg4
	  3. Jangalpur plant and segment-3 has no digit   -> "seg3-seg4"
	  4. otherwise                                    -> seg3
	Fewer than 3 segments: the string is already an ID, return it unchanged.

	IDEMPOTENT on its own output, which is what makes this safe to switch on over
	live data: every value it returns has fewer than 3 dash-segments, so decoding
	an already-decoded code returns it untouched. A row written under the old rule
	and re-decoded under the new one therefore cannot be mangled twice.
	"""
	s = (raw or "").strip()
	if not s:
		return ""
	parts = [p.strip() for p in s.split("-")]
	n = len(parts)

	def at(i):
		return parts[i - 1] if 1 <= i <= n else ""

	p1, p2, p3, p4 = at(1), at(2), at(3), at(4)

	if n >= 5 and p2.upper()[-3:] == "OEM":
		return f"{p3}-{p4}"
	if n >= 3:
		upper = p3.upper()
		if any(upper[:width] == expected for width, expected in _EXTRA_TESTS):
			return p4
		if "JANGALPUR" in p1.upper() and not re.search(r"[0-9]", p3):
			return f"{p3}-{p4}"
		return p3
	return s


def _decode(raw, rule_field, regex_field, label):
	if raw is None:
		return ""
	raw = str(raw).strip()
	if not raw:
		return ""
	rule = frappe.db.get_single_value("WMSLite Settings", rule_field) or "Identity"
	if rule == "Inventory Barcode":
		return extract_inventory_batch(raw)
	if rule == "Regex Extract":
		pattern = frappe.db.get_single_value("WMSLite Settings", regex_field)
		if pattern:
			try:
				m = re.search(pattern, raw)
				if m and m.groups():
					return m.group(1).strip()
			except re.error:
				frappe.log_error(f"Invalid {regex_field}: {pattern}", label)
		return raw
	return raw


def decode_coil_id(raw: str) -> str:
	"""Return the coil ID extracted from a raw scanned coil QR string."""
	return _decode(raw, "qr_decode_rule", "qr_decode_regex", "WMSLite QR decode")


def decode_bin_code(raw: str) -> str:
	"""Return the bin code extracted from a raw scanned bin QR string."""
	return _decode(raw, "bin_decode_rule", "bin_decode_regex", "WMSLite bin decode")
