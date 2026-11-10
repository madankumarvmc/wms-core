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

	Split on "-", trim each segment (collapsing runs of spaces, as AppSheet's
	TRIM does — the QR prints "mm  X" with two spaces), then:
	  1. >=5 segments, segment-2 ends "OEM" AND segment-4 is a short suffix
	     (<=3 chars, e.g. "464-C" corrugated sheet)   -> "seg3-seg4"
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
	parts = [re.sub(r"\s+", " ", p.strip()) for p in s.split("-")]
	n = len(parts)

	def at(i):
		return parts[i - 1] if 1 <= i <= n else ""

	p1, p2, p3, p4 = at(1), at(2), at(3), at(4)

	# The suffix guard is what keeps "PPGL Coil OEM-1R6I31513 - 1.000 mm X 914 X
	# 617 Mtr. - ..." from becoming "1R6I31513-1.000 mm X 914 X 617 Mtr.": there
	# segment-4 is the size, not part of the batch. SAP truncates BATCH to 10
	# chars, so that glued identity was being recorded in SAP as "1R6I31513-".
	# Mirrors the AppSheet formula's LEN(TRIM(INDEX(..., 4))) <= 3 (2026-10-02).
	if n >= 5 and p2.upper()[-3:] == "OEM" and len(p4) <= 3:
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


# The weight printed on a plant QR: "... - 2.800 MT- GCS0300", "... - 2.804 TO-",
# "...- X-3410.000 KG". Found by its unit, not by '-' position: the label layouts
# differ in segment count (plant prefix, extra codes, Jangalpur's
# "PGI0550120S/C" second segment), and a positional read lands on the size there.
# The size ("0.300 mm X 800 X 1830 mm") has no MT/TO/KG unit, so it never matches.
_QR_WEIGHT = re.compile(r"(\d+(?:\.\d+)?)\s*(MT|TO|KG)\b", re.IGNORECASE)


def qr_weight_mt(raw):
	"""Weight in metric tonnes read from a coil QR string, or None.

	MT and TO (tonnes) are taken as printed; KG is converted (/1000). The LAST
	weight token wins, as the weight is the last measured field on every label.
	A plain barcode (no unit token) returns None so callers fall back to the
	stored weight.
	"""
	matches = _QR_WEIGHT.findall(raw or "")
	if not matches:
		return None
	value, unit = matches[-1]
	w = float(value)
	return round(w / 1000.0, 6) if unit.upper() == "KG" else w


def qr_size(raw):
	"""The size printed on a coil QR label, or None.

	Positional, by owner decision (2026-11-03): split the label on every '-',
	trim each part and collapse inner spaces; the size is the FOURTH part.
	Written for the standard label

		5100 - PPGL Coil-1R6K34563 - 0.500 mm  X 1220 X 1041 Mtr. - 4.220 MT- PGL0500070S/RGW
		[1]    [2]       [3]         [4] = "0.500 mm X 1220 X 1041 Mtr."

	and applied to every label regardless: a layout with an extra '-' before the
	size ("... PGL Coil FG -P1B-1R4G21195 - ...") returns the wrong part. That is
	accepted (116 of 18,660 QRs on the client's site). Fewer than four parts —
	a plain barcode — returns None so callers fall back to the stored size.
	"""
	parts = [re.sub(r"\s+", " ", p).strip() for p in (raw or "").split("-")]
	if len(parts) < 4:
		return None
	return parts[3] or None

