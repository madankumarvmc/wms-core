# Copyright (c) 2026, StackBox and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from sbx_wmslite import classify


class CoilTransaction(Document):
	"""Append-only log of one coil scan (as received from the source feed).

	Raw transactions are derived into the current Bin Inventory by
	sbx_wmslite.bin_api.process_pending_transactions (latest scan wins).
	"""

	def before_insert(self):
		# Stored classification for the console's global filters. Every insert
		# path (SAP feed intake, count scan, RM ledger, uploads, tests) goes
		# through doc.insert(), so this one hook covers them all; the table is
		# append-only, so nothing needs re-deriving on update. See classify.py.
		if not self.site:
			self.site = classify.site_for_bin(self.bin_code or self.rm_destination)
		if not self.material_type:
			product = self.product
			if not product and self.coil_barcode:
				# Handheld scans carry no product: borrow the coil's inventory
				# record. A brand-new coil's first count has none yet and lands
				# as Unassigned until the next backfill — documented, accepted.
				product = frappe.db.get_value("Bin Inventory", self.coil_barcode, "product")
			self.material_type = classify.material_for_product(product)
