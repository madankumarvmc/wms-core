"""decode.qr_weight_mt — the weight printed on a coil's QR label, in MT.

Pure function, no fixtures. Every string below is a real label scanned on
site; the layouts differ in segment count, which is why the weight is found by
its unit and not by '-' position.

    bench --site <site> run-tests --app sbx_wmslite --module sbx_wmslite.tests.test_qr_weight
"""

import unittest

from sbx_wmslite.decode import qr_weight_mt


class TestQrWeight(unittest.TestCase):
	def test_real_labels(self):
		cases = [
			("1200 - GC Sheet-26AM272 - 0.300 mm  X 800 X 1830 mm - 2.800 MT- GCS0300", 2.8),
			("1200 - GL Corrugated Sheet-26AL290 - 0.250 mm  X 780 X 2135 mm - 2.140 MT- GL0250070/C", 2.14),
			("1200 - H.R.COIL-R6JWH6230 - 3.300 mm  X 1260 - 24.280 MT- H033001260C2", 24.28),
			("5100 - PPGL Coil-1R6J33290 - 0.500 mm  X 1220 X 1176 Mtr. - 4.730 MT- PGL0500070S/RGW", 4.73),
			("Jangalpur CR3 - H.R.COIL-JJG40788 - 2.500 mm  X 1230 - 26.420 MT- H025001230C2", 26.42),
			# tonnes spelled TO, and a second segment that shifts a positional parse
			("Jangalpur CR3 - PGI0550120S/C - PGI SRG.Corr. 0.55 mm/120GSM-591-C - "
			 "0.550 mm  X 800 X 2438 mm - 2.804 TO-", 2.804),
			# KG labels are converted to MT
			("Colour Coating Plant-PPGL COIL-1R6J32566- X-3410.000 KG", 3.41),
			("Jangalpur Al Foil - ALUMINIUM FOIL BARE-J2603B111 - 6.000 mm  X 1280 - 720.500 KG- UL06000", 0.7205),
		]
		for qr, expected in cases:
			with self.subTest(qr=qr):
				self.assertAlmostEqual(qr_weight_mt(qr), expected, places=6)

	def test_size_is_never_read_as_weight(self):
		# "0.300 mm" and "1176 Mtr." carry no MT/TO/KG unit
		self.assertIsNone(qr_weight_mt("GC Sheet - 0.300 mm  X 800 X 1830 mm"))
		self.assertIsNone(qr_weight_mt("PPGL Coil - 0.500 mm  X 1220 X 1176 Mtr."))

	def test_plain_barcode_has_no_weight(self):
		for raw in ("JJG43317", "26AM272", "", None):
			with self.subTest(raw=raw):
				self.assertIsNone(qr_weight_mt(raw))
