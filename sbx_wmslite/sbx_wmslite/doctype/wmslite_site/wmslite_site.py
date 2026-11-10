# Copyright (c) 2026, StackBox and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from sbx_wmslite import classify


class WMSLiteSite(Document):
	"""A site (plant / yard) for the console's global Site filter.

	The site of anything in the warehouse is DERIVED from its bin-code prefix
	through this master; Loading Plans (no bin) go through the SAP plant codes.
	Editing a site re-classifies the Coil Transaction ledger in the background —
	the small tables derive at query time and reflect the change at once.
	"""

	def validate(self):
		classify.validate_site_prefixes(self)

	def on_update(self):
		classify.clear_cache()
		classify.enqueue_backfill()

	def on_trash(self):
		classify.clear_cache()
		classify.enqueue_backfill()
