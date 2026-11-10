"""Handheld Inventory lookup — coil + location, read-only.

One scan-or-type box, one list. The box takes EITHER a bin location (then the
list is every coil in that bin) OR a coil barcode / full coil QR (then the list
is that one coil and where it is). Nothing is written, so there is no offline
queue involvement: the screen simply needs a connection.

RESOLVING A STRING THAT COULD BE EITHER
---------------------------------------
No other flow has this problem — each knows what it expects. The order matters:

  1. A descriptive coil QR always contains " - " (`locations.COIL_MARKER`); a
     location label never does. Such a string skips the bin check entirely.
  2. Otherwise try it as a BIN first: `decode_bin_code`, then "is it a known
     location (WMSLite Bin) or does any Bin Inventory row sit in it?". Bin first
     because 85 real labels contain dashes (`RP/S45/AB/A1-A2/R1-6`) and the coil
     rule would happily chop one of those down to "6".
  3. Then as a COIL: `decode_coil_id`, then "does that Bin Inventory row exist?".
  4. Neither: a prefix search on both columns (`q%`, index-backed on each), so a
     half-typed code still narrows the list. The RAW text is used here, never a
     decoded form — the decoders are for full labels, not fragments.

PAGING
------
Ordered by bin then coil, so scrolling reads like a walk through the warehouse.
Keyset paging (`after_bin`/`after_coil` = the last row shown) over the composite
(bin_code, coil_barcode) index (patch v15): every "Show more" is an index range
scan however deep the page, and a feed insert between pages can neither repeat
nor skip a row — OFFSET paging does both.
"""

import frappe
from frappe import _
from frappe.utils import cint

from sbx_wmslite import locations, tasks
from sbx_wmslite.decode import decode_bin_code, decode_coil_id

DEFAULT_PAGE = 20
MAX_PAGE = 100

_FIELDS = ("coil_barcode, bin_code, material_grade, weight, product, status, qc, "
		   "rm_status, rm_destination, updated_at")


def _require():
	tasks.require(tasks.INVENTORY_ROLES, "inventory_view_enabled", _("Inventory"))


def _page_size(limit):
	n = cint(limit) or cint(frappe.db.get_single_value("WMSLite Settings", "inventory_page_size")) \
		or DEFAULT_PAGE
	return max(1, min(n, MAX_PAGE))


def resolve_query(raw):
	"""What the operator meant: {"mode": all|bin|coil|prefix, "value": ...}."""
	raw = (raw or "").strip()
	if not raw:
		return {"mode": "all", "value": ""}
	if locations.COIL_MARKER not in raw:
		code = (decode_bin_code(raw) or "").strip()
		if code and (locations.is_known(code) or frappe.db.exists("Bin Inventory", {"bin_code": code})):
			return {"mode": "bin", "value": code}
	coil = (decode_coil_id(raw) or "").strip()
	if coil and frappe.db.exists("Bin Inventory", coil):
		return {"mode": "coil", "value": coil}
	return {"mode": "prefix", "value": raw}


def _where(res, vals):
	mode = res["mode"]
	if mode == "bin":
		vals["v"] = res["value"]
		return "bin_code = %(v)s"
	if mode == "coil":
		vals["v"] = res["value"]
		return "coil_barcode = %(v)s"
	if mode == "prefix":
		vals["v"] = res["value"] + "%"
		return "(coil_barcode LIKE %(v)s OR bin_code LIKE %(v)s)"
	return "1 = 1"


@frappe.whitelist()
def inventory_lookup(q=None, after_bin=None, after_coil=None, limit=None):
	"""One page of Bin Inventory for the handheld Inventory card.

	`q` is the scanned/typed text (see resolve_query). `after_bin` + `after_coil`
	are the last row of the previous page ("Show more"); omit them for page one.
	Returns {ok, mode, value, rows, total, has_more, next}, where `next` is the
	pair to send back for the following page (None when there is none).
	"""
	_require()
	res = resolve_query(q)
	vals = {}
	where = _where(res, vals)
	total = cint(frappe.db.sql(f"SELECT COUNT(*) FROM `tabBin Inventory` WHERE {where}", vals)[0][0])

	page = _page_size(limit)
	if after_bin or after_coil:
		where += " AND (bin_code, coil_barcode) > (%(ab)s, %(ac)s)"
		vals["ab"], vals["ac"] = after_bin or "", after_coil or ""
	rows = frappe.db.sql(f"""
		SELECT {_FIELDS} FROM `tabBin Inventory`
		WHERE {where}
		ORDER BY bin_code ASC, coil_barcode ASC
		LIMIT %(lim)s""", dict(vals, lim=page + 1), as_dict=True)   # +1: is there a next page?
	has_more = len(rows) > page
	rows = rows[:page]
	for r in rows:
		r["qc"] = cint(r.qc)
		r["updated_at"] = str(r.updated_at) if r.updated_at else None
	nxt = ({"after_bin": rows[-1].bin_code, "after_coil": rows[-1].coil_barcode}
		   if rows and has_more else None)
	return {"ok": 1, "mode": res["mode"], "value": res["value"], "rows": rows,
			"total": total, "has_more": has_more, "next": nxt}
