# Copyright (c) 2026, StackBox and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CoilTransaction(Document):
	"""Append-only log of one coil scan (as received from the source feed).

	Raw transactions are derived into the current Bin Inventory by
	sbx_wmslite.bin_api.process_pending_transactions (latest scan wins).
	"""

	pass
