"""decode.qr_size — the size printed on a coil's QR label.

Pure function, no fixtures. The rule is positional on purpose (owner, 2026-11-03):
split on every '-', the size is the 4th part. The last case pins the accepted
mis-read for labels with an extra '-' before the size, so it stays deliberate.

    bench --site <site> run-tests --app sbx_wmslite --module sbx_wmslite.tests.test_qr_size
"""

import unittest

from sbx_wmslite.decode import qr_size


class TestQrSize(unittest.TestCase):
	def test_standard_labels(self):
		cases = [
			("5100 - PPGL Coil-1R6K34563 - 0.500 mm  X 1220 X 1041 Mtr. - 4.220 MT- PGL0500070S/RGW",
			 "0.500 mm X 1220 X 1041 Mtr."),
			("005100 - PPGL Coil-3R6J18223 - 0.450 mm  X 1220 X 1192 Mtr. - 4.320 MT- PGL0450070S/RGW",
			 "0.450 mm X 1220 X 1192 Mtr."),
			("1200 - GC Sheet-26AM272 - 0.300 mm  X 800 X 1830 mm - 2.800 MT- GCS0300",
			 "0.300 mm X 800 X 1830 mm"),
			("1200 - H.R.COIL-R6JWH6230 - 3.300 mm  X 1260 - 24.280 MT- H033001260C2",
			 "3.300 mm X 1260"),
		]
		for qr, expected in cases:
			with self.subTest(qr=qr):
				self.assertEqual(qr_size(qr), expected)

	def test_plain_barcode_or_short_string_has_no_size(self):
		for raw in ("JJG43317", "2R6J45375", "a-b-c", "", None):
			with self.subTest(raw=raw):
				self.assertIsNone(qr_size(raw))

	def test_extra_dash_layouts_are_misread_by_design(self):
		# Accepted: the rule is applied to every label, so an extra '-' before the
		# size shifts the 4th part onto the batch code.
		self.assertEqual(qr_size("Colour Coating Plant - PGL Coil FG -P1B-1R4G21195 - "
								 "0.450 mm  X 1220 X 1013 Mtr. - 4.190 MT- PGL0450070BS/RGW"), "1R4G21195")
