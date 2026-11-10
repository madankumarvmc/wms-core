# Copyright (c) 2026, StackBox and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class WMSLiteImportBatch(Document):
	"""One console upload (Loading Plan / Bin Inventory / Bin Master) and its outcome.

	Was "Import Batch" until 2026-11-04. That name is also defined by the Inventory
	Recording and Yard Management apps; a doctype name is site-wide, so whichever
	app migrated last owned the table, and Inventory Recording's node hooks fired
	on WMS uploads ("Select a node first."). Patch v16 moved the rows here.
	"""

	pass
