"""The Python batch-code rule and the handheld's JS copy must agree, always.

There are two implementations of the same rule and there has to be: the server
decides identity, and the handheld needs the same answer offline, where it cannot
ask. They diverged once already — the JS extracted `JJG40788` while the server
stored the whole 78-character QR — and the result was duplicate coil records and
a line that could not receive its own stock.

So this file is the contract. It runs BOTH implementations over one corpus and
asserts they return the same string for every input. The JS is executed with
`node`, the same way the app's other JS is syntax-checked; if node is missing the
JS half skips rather than silently passing.

The corpus is real: every descriptive format seen in the live data, plus the
edge cases the rule's branches exist for.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from frappe.tests.utils import FrappeTestCase

from sbx_wmslite.decode import extract_inventory_batch

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PWA = os.path.join(APP_ROOT, "www", "wms-loading.html")

# Real strings from the plant, then the branch cases.
CORPUS = [
	# --- as scanned in production ---
	"Jangalpur CR3 - H.R.COIL-JJG40788 - 2.500 mm  X 1230 - 26.420 MT- H025001230C2",
	"CR Plant Ranihati - H.R.COIL-R6EJH2290 - 2.000 mm  X 1330 - 22.119 MT- H020001330C2",
	"Jangalpur Al Foil - ALUMINIUM FOIL BARE-DIAGLONGCOIL0001 - 6.000 mm  X 1000 - 535.000 KG- UL06000",
	"Colour Coating Plant - PPGL Coil-2R5K50229 - 0.400 mm  X 1220 X 1274 Mtr. - 4.096 MT- PGL0400070S/RGW",
	"Colour Coating Plant - PPGL Coil OEM-1R6C08506 - 0.500 mm  X 1100 X 1043 Mtr. - 3.935 MT- PGL0500150O/RW",
	"Colour Coating Plant - CCL Coil SFG-1R6E15270 - 0.400 mm  X 1220 X 1403 Mtr. - 4.765 MT- SPGL0400070154RM",
	"Colour Coating Plant - PPGL Coil FG P2.-1R6H26073 - 0.350 mm  X 1220 X 937 Mtr. - 2.630 MT- PGL03500702S/RGW",
	"Jangalpur CR3 - GC Sheet-F5280 - 0.350 mm  X 800 X 2745 mm - 2.856 MT- GCS0350",
	"Jangalpur CR3 - GL Coil-A055191 - 0.450 mm  X 1320 X 1018 Mtr. - 3.955 MT- GL04501320070/RW",
	"Jangalpur CR3 - TRIMMED CR COIL-T4J6G0064 - 0.275 mm  X 1000 - 19.035 MT- TCR02751000",
	"PANCHALA - GALVANIZED W BEAM.-G0923CI016 - 1.800 mm  - 1.207 MT- - No of pieces : 40 - GWB181",
	# --- already-decoded values: the rule must be idempotent on its own output ---
	"JJG40788",
	"R6EJH2290",
	"6ITP6806",
	"120239",
	"H025001230C2",
	# --- branch coverage ---
	"PlantX - SOMETHINGOEM-BATCH1 - a - b - c",      # rule 1: seg2 ends OEM
	"PlantX - Thing-P2ABC - seg4here - c - d",        # rule 2: extra code -> seg4
	"PlantX - Thing-GL CODE - seg4here - c - d",      # rule 2, prefix with a space
	"PlantX - Thing-PGI CODE - seg4here - c",         # rule 2, 4-char prefix
	"Jangalpur CR3 - Thing-ABCDEF - seg4here",        # rule 3: Jangalpur, no digit
	"Jangalpur CR3 - Thing-ABC123 - seg4here",        # rule 3 blocked by the digit
	"PlantX - Thing-BATCH9 - tail",                   # rule 4: default seg3
	# --- the dead RM branch: must NOT be treated as an extra code ---
	"PlantX - Thing-RM123 - seg4here - c",
	# --- degenerate ---
	"",
	"   ",
	"NODASHES",
	"ONE-TWO",
	"A-B-",
	"  padded - value - here  ",
]


def _js_source():
	"""Pull decodeInventoryBarcode out of the PWA by brace-matching."""
	html = open(PWA).read()
	start = html.index("function decodeInventoryBarcode")
	depth, started = 0, False
	for i in range(start, len(html)):
		if html[i] == "{":
			depth += 1
			started = True
		elif html[i] == "}":
			depth -= 1
			if started and depth == 0:
				return html[start:i + 1]
	raise AssertionError("decodeInventoryBarcode not found in wms-loading.html")


class TestDecodeParity(FrappeTestCase):
	def test_python_rule_is_idempotent(self):
		"""Decoding an already-decoded value must return it unchanged.

		This is what makes switching the rule on over live data safe: a row written
		before the switch cannot be mangled by being decoded a second time.
		"""
		for raw in CORPUS:
			once = extract_inventory_batch(raw)
			self.assertEqual(extract_inventory_batch(once), once,
							 f"not idempotent for {raw!r}")

	def test_dead_rm_branch_is_preserved(self):
		"""`RM` must not be treated as an extra code — the JS cannot match it.

		Guards against someone 'tidying' the prefix test into startswith(), which
		would re-identify every RM-prefixed coil and split it from its stored row.
		"""
		self.assertEqual(
			extract_inventory_batch("PlantX - Thing-RM123 - seg4here - c"), "RM123")

	def test_js_and_python_agree(self):
		node = shutil.which("node")
		if not node:
			raise unittest.SkipTest("node not available — JS half of the parity check skipped")

		# Corpus goes in over stdin, not argv: it only grows, and a long enough
		# argument list would start failing on ARG_MAX rather than on a mismatch.
		script = _js_source() + """
let stdin = "";
process.stdin.on("data", (d) => { stdin += d; });
process.stdin.on("end", () => {
	process.stdout.write(JSON.stringify(JSON.parse(stdin).map(decodeInventoryBarcode)));
});
"""
		with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
			fh.write(script)
			path = fh.name
		try:
			proc = subprocess.run([node, path], input=json.dumps(CORPUS),
								  capture_output=True, text=True, timeout=60)
			self.assertEqual(proc.returncode, 0, proc.stderr)
			js_results = json.loads(proc.stdout)
		finally:
			os.unlink(path)

		self.assertEqual(len(js_results), len(CORPUS))
		mismatches = [
			(raw, js, extract_inventory_batch(raw))
			for raw, js in zip(CORPUS, js_results)
			if js != extract_inventory_batch(raw)
		]
		self.assertEqual(mismatches, [], "JS and Python disagree:\n" + "\n".join(
			f"  {raw!r}\n    js={js!r}\n    py={py!r}" for raw, js, py in mismatches))
