"""Coil QR decoding.

SAP sends a coil barcode (the match key). The physical coil QR may encode extra
data (GS1 / embedded fields); we extract the coil ID from it before matching.

This is intentionally a thin, configurable seam — the real GS1-128 / custom
format drops in here without touching any flow logic. Rule is read from
WMSLite Settings:

  * Identity      — the scanned QR string IS the coil ID (trimmed).
  * Regex Extract — capture group 1 of `qr_decode_regex` is the coil ID.
"""

import re

import frappe


def _decode(raw, rule_field, regex_field, label):
	if raw is None:
		return ""
	raw = str(raw).strip()
	if not raw:
		return ""
	rule = frappe.db.get_single_value("WMSLite Settings", rule_field) or "Identity"
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
