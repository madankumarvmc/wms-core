# Copyright (c) 2026, StackBox and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class WMSLiteBin(Document):
	"""A scannable location: a storage bin or a production line.

	Exists so a scanned location can be VALIDATED rather than taken on trust. Before
	it, any string scanned into "Scan the destination line" became a destination —
	including a coil QR, which is how a coil barcode ended up as a bin code with
	real stock parked in it.
	"""

	def validate(self):
		self.bin_code = (self.bin_code or "").strip()
