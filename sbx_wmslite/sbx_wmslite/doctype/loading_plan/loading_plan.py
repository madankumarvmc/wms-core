import frappe
from frappe.model.document import Document

# Statuses that mean a plan is still actionable on the floor.
OPEN_STATES = ("Open", "In Progress", "Ready to Complete", "Pending Approval")
# Terminal statuses — a re-send must not silently reopen these.
CLOSED_STATES = ("Completed", "Completed (Short)", "Cancelled")


COMPLETE_STATES = ("Completed", "Completed (Short)")


class LoadingPlan(Document):
	def validate(self):
		self.recompute_counts()

	def on_update(self):
		"""Fire the outbound SAP goods-issue confirmation when a plan first
		transitions into a completed state. Centralised here so every path
		(operator confirm, short-load approval, console recomplete) is covered."""
		before = self.get_doc_before_save()
		was_complete = bool(before and before.plan_status in COMPLETE_STATES)
		if self.plan_status in COMPLETE_STATES and not was_complete:
			from sbx_wmslite import outbound_sap

			outbound_sap.on_plan_completed(self)
			# Release any Deferred T1 loading pushes now that the plan is complete.
			# Centralised here so EVERY completion path (operator complete, console
			# complete, short-load approval) arms the deferred coils exactly once.
			# A failure here must never block completion — the PGI is the critical
			# side-effect; the T1 sweep can be re-armed from the console.
			try:
				from sbx_wmslite import loading_push

				loading_push.arm_deferred(self.name)
			except Exception:
				frappe.log_error(frappe.get_traceback(),
								 "WMSLite arm_deferred on completion failed")

	@staticmethod
	def _auto_complete_enabled():
		"""Global 'auto-complete when all coils loaded' switch. Defaults OFF — a
		fully-loaded plan waits at 'Ready to Complete' for a manual Complete. Unset
		(a freshly-migrated Single returns None until first saved) is treated as OFF."""
		val = frappe.db.get_single_value("WMSLite Settings", "auto_complete_on_full_load")
		return False if val is None else bool(int(val))

	def recompute_counts(self):
		"""Recompute coil tallies and roll the plan status forward.

		Status is derived, never hand-set on the floor:
		  - no loads yet              -> Open
		  - some loaded, some pending -> In Progress
		  - nothing pending           -> Completed (or Completed (Short) if any skipped)
		Pending Approval / Cancelled are set explicitly by their flows and are
		left untouched here.

		When auto-completion is switched off in WMSLite Settings, a fully-loaded
		plan stops at 'Ready to Complete' instead of completing — the operator must
		press Complete on the handheld (or a supervisor from the console), which
		sets `flags.operator_completing` so the completion below is allowed to fire.
		"""
		total = len(self.coils)
		loaded = sum(1 for c in self.coils if c.coil_status == "Loaded")
		skipped = sum(1 for c in self.coils if c.coil_status == "Skipped")
		pending = total - loaded - skipped

		self.total_coils = total
		self.loaded_coils = loaded
		self.skipped_coils = skipped
		self.pending_coils = pending

		# Don't override explicitly-managed terminal/approval states.
		if self.plan_status in ("Cancelled", "Pending Approval"):
			return

		if pending == 0 and total > 0:
			if self._auto_complete_enabled() or self.flags.get("operator_completing"):
				self.plan_status = "Completed (Short)" if skipped else "Completed"
			else:
				# Fully loaded but auto-complete is off — hold for a manual Complete.
				self.plan_status = "Ready to Complete"
		elif loaded > 0 or skipped > 0:
			self.plan_status = "In Progress"
		else:
			self.plan_status = "Open"

		# Stamp the completion time once, clear it if the plan is reopened — this
		# is the date dimension the dashboard's "completed in range" tile uses.
		if self.plan_status in COMPLETE_STATES:
			if not self.completed_at:
				self.completed_at = frappe.utils.now_datetime()
		else:
			self.completed_at = None
