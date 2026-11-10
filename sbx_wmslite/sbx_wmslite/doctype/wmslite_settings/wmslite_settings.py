import frappe
from frappe.model.document import Document

from sbx_wmslite import classify


class WMSLiteSettings(Document):
	def on_update(self):
		# The pipe keywords feed the console's Material Type classification;
		# the rules are cached, and the Coil Transaction ledger stores its result.
		classify.clear_cache()
		before = self.get_doc_before_save()
		if before and (before.get("material_pipe_keywords") or "") != (self.material_pipe_keywords or ""):
			classify.enqueue_backfill()
