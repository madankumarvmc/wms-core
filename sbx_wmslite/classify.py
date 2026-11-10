"""Site and material-type classification for the console's global filters.

Nothing in the data model carries a site or a material type. Both are DERIVED:

  * Site comes from the bin-code prefix (the leading alphanumeric token of the
    label: `JPR/KMILL/H2` -> `JPR`, `CCL2 /G18/ 36` -> `CCL2`). The prefix -> site
    mapping lives in the `WMSLite Site` master so a supervisor can extend it
    without a deploy. Loading Plans carry no bin, only an SAP plant code
    (`source_plant`), so the master also lists each site's plant codes.
  * Material type comes from the product name: a product containing one of the
    pipe keywords in WMSLite Settings is "Pipes", any other non-empty product is
    "Sheet (Coil)", a blank product is "Unassigned".

Where the derivation runs:
  * Query time, on the small tables (Bin Inventory ~81k rows, WMSLite Bin,
    Loading Plan): `site_where` / `material_where` / `site_sql` / `material_sql`
    build SQL fragments from the cached rules, so a mapping edit is visible at
    once. Measured <= 0.3 s over 81k rows.
  * Insert time, on Coil Transaction (~404k rows, append-only): the same rule
    evaluated in Python by `CoilTransaction.before_insert` into the stored,
    indexed `site` / `material_type` columns. Deriving at query time there cost
    17 s for an all-time filter. A mapping or keyword edit re-runs
    `backfill_transactions` in the background (`enqueue_backfill`).

Every SQL fragment takes its values through the `vals` dict rather than inline:
these fragments end up inside `frappe.db.sql(query, vals)`, where a literal `%`
is a format error.
"""

import re

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime

UNASSIGNED = "Unassigned"
MAT_SHEET = "Sheet (Coil)"
MAT_PIPES = "Pipes"
MATERIAL_TYPES = (MAT_SHEET, MAT_PIPES, UNASSIGNED)

# The dashboard's "Transaction Type" selector: (key, label). "" = all groups.
TXN_GROUPS = (("", "All"), ("inventory", "Inventory scanning"),
			  ("rm", "RM Movement"), ("loading", "Loading (Outbound)"))

DEFAULT_PIPE_KEYWORDS = "PIPE,TUBE"
CACHE_KEY = "sbx_wmslite:classify_rules"
BACKFILL_JOB = "sbx_wmslite.classify.backfill_transactions"

# Leading alphanumeric token. Deliberately NOT "up to the first /": the live
# labels include `CCL2 /G18`, `CCL1 D28/x` and `CCL1-D14`, which all belong to
# the same prefix family. Must stay in step with `prefix_sql`.
_PREFIX_RE = re.compile(r"[A-Za-z0-9]+")
_PREFIX_SQL_PATTERN = "^[A-Za-z0-9]+"

# Seed data (owner, 2026-10-08). 1200 / 5100 are inferred from Bin Inventory.plant
# (1200 only ever sits in JPR bins, 5100 only in CC/CCL bins); 1300 and 5200 are
# deliberately left unmapped until the client confirms which site they are.
SEED_SITES = [
	{"site_name": "Jangalpur", "sort_order": 1, "prefixes": ["JPR"], "plant_codes": "1200"},
	{"site_name": "Ranihati", "sort_order": 2, "prefixes": ["CC1", "CC2", "CCL1", "CCL2", "CCL3"],
	 "plant_codes": "5100"},
	{"site_name": "Pipe Division", "sort_order": 3, "prefixes": ["RP"], "plant_codes": ""},
]


# --------------------------------------------------------------------------
# rules (cached)
# --------------------------------------------------------------------------
def _split_csv(s):
	return [p.strip().upper() for p in (s or "").split(",") if p.strip()]


def load_rules():
	"""The classification rules, cached per site in Redis.

	Shape: {"sites": [{"name", "prefixes": [UPPER], "plant_codes": [str]}],
			"all_prefixes": [...], "all_plants": [...], "pipe_keywords": [UPPER]}
	Guarded against a pre-migrate call (the master not yet synced) so a Coil
	Transaction insert during migrate never fails on classification.
	"""
	rules = frappe.cache.get_value(CACHE_KEY)
	if rules:
		return rules
	sites = []
	if frappe.db.exists("DocType", "WMSLite Site"):
		rows = frappe.get_all("WMSLite Site", filters={"disabled": 0},
							  fields=["name", "plant_codes", "sort_order"],
							  order_by="sort_order asc, name asc")
		prefixes = frappe.get_all("WMSLite Site Prefix", fields=["parent", "bin_prefix"])
		by_site = {}
		for p in prefixes:
			if (p.bin_prefix or "").strip():
				by_site.setdefault(p.parent, []).append(p.bin_prefix.strip().upper())
		for r in rows:
			sites.append({"name": r.name, "prefixes": sorted(set(by_site.get(r.name, []))),
						  "plant_codes": [c.strip() for c in (r.plant_codes or "").split(",") if c.strip()]})
	kw = None
	if frappe.db.exists("DocType", "WMSLite Settings"):
		kw = frappe.db.get_single_value("WMSLite Settings", "material_pipe_keywords")
	rules = {
		"sites": sites,
		"all_prefixes": sorted({p for s in sites for p in s["prefixes"]}),
		"all_plants": sorted({c for s in sites for c in s["plant_codes"]}),
		"pipe_keywords": _split_csv(kw or DEFAULT_PIPE_KEYWORDS),
	}
	frappe.cache.set_value(CACHE_KEY, rules)
	return rules


def clear_cache():
	frappe.cache.delete_value(CACHE_KEY)


def site_options():
	"""Dropdown values: the master in its own order, then the catch-all."""
	return [s["name"] for s in load_rules()["sites"]] + [UNASSIGNED]


# --------------------------------------------------------------------------
# Python classification (insert-time + post-processing)
# --------------------------------------------------------------------------
def prefix_of(code):
	"""`JPR/KMILL/H2` -> `JPR`; `ccl2 /G18` -> `CCL2`; blank / no token -> None."""
	m = _PREFIX_RE.match((code or "").strip())
	return m.group(0).upper() if m else None


def site_for_bin(bin_code):
	p = prefix_of(bin_code)
	if p:
		for s in load_rules()["sites"]:
			if p in s["prefixes"]:
				return s["name"]
	return UNASSIGNED


def site_for_plant(plant_code):
	code = str(plant_code or "").strip()
	if code:
		for s in load_rules()["sites"]:
			if code in s["plant_codes"]:
				return s["name"]
	return UNASSIGNED


def material_for_product(product):
	p = (product or "").strip().upper()
	if not p:
		return UNASSIGNED
	if any(k in p for k in load_rules()["pipe_keywords"]):
		return MAT_PIPES
	return MAT_SHEET


# --------------------------------------------------------------------------
# SQL fragments (query-time classification on the small tables)
# --------------------------------------------------------------------------
def _key(vals, base):
	"""A params key not yet used in `vals` — one query may hold several fragments."""
	k, n = base, 1
	while k in vals:
		n += 1
		k = f"{base}_{n}"
	return k


def prefix_sql(col):
	"""SQL twin of `prefix_of` (pinned by test_classify's parity test)."""
	return f"UPPER(REGEXP_SUBSTR(TRIM({col}), '{_PREFIX_SQL_PATTERN}'))"


def site_sql(col, vals):
	"""`CASE ... END` yielding the site name for a bin-code column."""
	sites = [s for s in load_rules()["sites"] if s["prefixes"]]
	un = _key(vals, "cs_un")
	vals[un] = UNASSIGNED
	if not sites:
		return f"%({un})s"
	parts = []
	for s in sites:
		kp, kn = _key(vals, "cs_p"), _key(vals, "cs_n")
		vals[kp], vals[kn] = tuple(s["prefixes"]), s["name"]
		parts.append(f"WHEN {prefix_sql(col)} IN %({kp})s THEN %({kn})s")
	return "CASE " + " ".join(parts) + f" ELSE %({un})s END"


def site_where(col, site, vals):
	"""Predicate: the bin-code column `col` belongs to `site`."""
	rules = load_rules()
	if site == UNASSIGNED:
		if not rules["all_prefixes"]:
			return "1 = 1"
		k = _key(vals, "sw_all")
		vals[k] = tuple(rules["all_prefixes"])
		return f"({col} IS NULL OR {col} = '' OR {prefix_sql(col)} NOT IN %({k})s)"
	for s in rules["sites"]:
		if s["name"] == site:
			if not s["prefixes"]:
				return "1 = 0"
			k = _key(vals, "sw_p")
			vals[k] = tuple(s["prefixes"])
			return f"{prefix_sql(col)} IN %({k})s"
	return "1 = 0"   # a site the master does not know


def plant_where(col, site, vals):
	"""Predicate: the SAP plant-code column `col` belongs to `site` (Loading Plans)."""
	rules = load_rules()
	if site == UNASSIGNED:
		if not rules["all_plants"]:
			return "1 = 1"
		k = _key(vals, "pw_all")
		vals[k] = tuple(rules["all_plants"])
		return f"({col} IS NULL OR {col} = '' OR {col} NOT IN %({k})s)"
	for s in rules["sites"]:
		if s["name"] == site:
			if not s["plant_codes"]:
				return "1 = 0"
			k = _key(vals, "pw_p")
			vals[k] = tuple(s["plant_codes"])
			return f"{col} IN %({k})s"
	return "1 = 0"


def _pipe_pred(col, vals):
	kws = load_rules()["pipe_keywords"]
	if not kws:
		return "1 = 0"
	parts = []
	for kw in kws:
		k = _key(vals, "mw_k")
		vals[k] = f"%{kw}%"
		parts.append(f"UPPER({col}) LIKE %({k})s")
	return "(" + " OR ".join(parts) + ")"


def material_where(col, material_type, vals):
	"""Predicate: the product column `col` is of `material_type`."""
	if material_type == MAT_PIPES:
		return f"({col} IS NOT NULL AND {col} <> '' AND {_pipe_pred(col, vals)})"
	if material_type == MAT_SHEET:
		return f"({col} IS NOT NULL AND {col} <> '' AND NOT {_pipe_pred(col, vals)})"
	if material_type == UNASSIGNED:
		return f"({col} IS NULL OR {col} = '')"
	return "1 = 0"


def material_sql(col, vals):
	"""`CASE ... END` yielding the material type for a product column."""
	ku, kp, ks = _key(vals, "ms_un"), _key(vals, "ms_pipes"), _key(vals, "ms_sheet")
	vals[ku], vals[kp], vals[ks] = UNASSIGNED, MAT_PIPES, MAT_SHEET
	return (f"CASE WHEN {col} IS NULL OR {col} = '' THEN %({ku})s "
			f"WHEN {_pipe_pred(col, vals)} THEN %({kp})s ELSE %({ks})s END")


# --------------------------------------------------------------------------
# seed + backfill
# --------------------------------------------------------------------------
def seed_sites():
	"""Create the seed sites that do not exist yet. Additive and idempotent, like
	`locations.seed_from_fixture`: an edited site is never overwritten."""
	created = 0
	frappe.flags.wms_skip_backfill = True
	try:
		for spec in SEED_SITES:
			if frappe.db.exists("WMSLite Site", spec["site_name"]):
				continue
			doc = frappe.new_doc("WMSLite Site")
			doc.site_name = spec["site_name"]
			doc.sort_order = spec["sort_order"]
			doc.plant_codes = spec["plant_codes"]
			for p in spec["prefixes"]:
				doc.append("prefixes", {"bin_prefix": p})
			doc.insert(ignore_permissions=True)
			created += 1
	finally:
		frappe.flags.wms_skip_backfill = False
	clear_cache()
	return {"created": created, "seeded": len(SEED_SITES)}


def _month_windows():
	"""[(start, end)] calendar-month windows covering every scanned_at, so a
	404k-row UPDATE is many short statements instead of one long lock."""
	lo, hi = frappe.db.sql("SELECT MIN(scanned_at), MAX(scanned_at) FROM `tabCoil Transaction`")[0]
	if not lo:
		return []
	cur = get_datetime(lo).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
	hi = get_datetime(hi)
	out = []
	while cur <= hi:
		nxt = add_to_date(cur, months=1)
		out.append((cur, nxt))
		cur = nxt
	return out


def backfill_transactions(only_null=False):
	"""Recompute `site` / `material_type` on every Coil Transaction.

	Same rule as `CoilTransaction.before_insert`: site from the row's own bin code
	(else its RM destination); material from the row's product, else the coil's
	current Bin Inventory product (handheld rows carry no product). Commits per
	month window. Returns the number of rows touched.
	"""
	clear_cache()
	touched = 0
	site_col = "COALESCE(NULLIF(ct.bin_code, ''), ct.rm_destination)"
	only = " AND ct.site IS NULL" if only_null else ""
	windows = _month_windows() + [(None, None)]   # trailing window: NULL scanned_at
	for start, end in windows:
		vals = {"start": start, "end": end}
		win = ("ct.scanned_at >= %(start)s AND ct.scanned_at < %(end)s" if start
			   else "ct.scanned_at IS NULL")
		# rows that carry a product (the SAP feed): classify from the row itself
		v1 = dict(vals)
		frappe.db.sql(f"""
			UPDATE `tabCoil Transaction` ct
			SET ct.site = {site_sql(site_col, v1)},
				ct.material_type = {material_sql("ct.product", v1)}
			WHERE {win} AND COALESCE(ct.product, '') <> ''{only}""", v1)
		touched += cint(getattr(frappe.db._cursor, "rowcount", 0))
		# rows without one (handheld scans): borrow the coil's inventory product
		v2 = dict(vals)
		frappe.db.sql(f"""
			UPDATE `tabCoil Transaction` ct
			LEFT JOIN `tabBin Inventory` bi ON bi.coil_barcode = ct.coil_barcode
			SET ct.site = {site_sql(site_col, v2)},
				ct.material_type = {material_sql("bi.product", v2)}
			WHERE {win} AND COALESCE(ct.product, '') = ''{only}""", v2)
		touched += cint(getattr(frappe.db._cursor, "rowcount", 0))
		frappe.db.commit()
	return touched


def enqueue_backfill():
	"""Re-classify the ledger in the background after a mapping / keyword edit.
	Skipped while seeding (the caller backfills once itself) and in tests."""
	if frappe.flags.wms_skip_backfill or frappe.flags.in_test or frappe.flags.in_migrate:
		return
	frappe.enqueue(BACKFILL_JOB, queue="long", timeout=1800, enqueue_after_commit=True,
				   job_id=BACKFILL_JOB, deduplicate=True)


def validate_site_prefixes(doc):
	"""Normalise a WMSLite Site's prefixes and refuse one another site owns."""
	seen = set()
	for row in doc.get("prefixes") or []:
		p = prefix_of(row.bin_prefix)
		if not p or p != (row.bin_prefix or "").strip().upper():
			frappe.throw(_("Prefix '{0}' must be a single alphanumeric token, e.g. JPR or CCL2")
						 .format(row.bin_prefix))
		if p in seen:
			frappe.throw(_("Prefix '{0}' is listed twice").format(p))
		seen.add(p)
		row.bin_prefix = p
		owner = frappe.db.get_value("WMSLite Site Prefix",
									{"bin_prefix": p, "parent": ("!=", doc.name or "")}, "parent")
		if owner:
			frappe.throw(_("Prefix '{0}' already belongs to site '{1}'").format(p, owner))
	doc.plant_codes = ",".join(c.strip() for c in (doc.plant_codes or "").split(",") if c.strip())
