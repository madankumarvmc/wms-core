"""Snapshot and restore WMSLite Settings around a test class.

WMSLite Settings is a Single: there is exactly one row, and these suites run
against the same one the live site uses. The endpoints under test commit (which
is why the suites clean up by hand rather than relying on FrappeTestCase's
rollback), so whatever a test writes here is still there when the run ends.

That is not hypothetical. Runs against the production site left behind:
  * rm_unknown_bin = "__TEST__RM_UNKNOWN", which live gate scans then stamped
    onto real coils as their bin_code (see patch v8_rm_placeholder_bin);
  * the inbound SAP endpoint's shared secret replaced by its test value, while
    that endpoint was enabled;
  * the short-load approval PIN likewise;
  * loading_push_url / sap_token_url / sap_gi_url pointed at example hosts.

PASSWORD FIELDS
---------------
`sap_api_key`, `supervisor_pin`, `sap_client_secret` and
`loading_push_basic_password` are Password fields. Frappe keeps those out of the
doc: `doc.get(field)` hands back a masked placeholder, not the secret. Snapshotting
that placeholder and writing it back on restore would overwrite the real secret
with literal asterisks — worse than leaving the test value in place, and
unrecoverable. So password fields are read with `get_password()` and restored as
plaintext, and a field that cannot be read back is left alone rather than guessed
at.
"""

import frappe

DOCTYPE = "WMSLite Settings"
_MASK = "*"


def _password_fields():
	return {df.fieldname for df in frappe.get_meta(DOCTYPE).fields
			if df.fieldtype == "Password"}


class SettingsGuard:
	def __init__(self, *fields):
		self.fields = fields
		self._saved = {}

	def _read(self, doc, field, passwords):
		if field not in passwords:
			return doc.get(field)
		# Never snapshot the mask — restoring it would destroy the real secret.
		try:
			value = doc.get_password(field, raise_exception=False)
		except Exception:
			return None
		if value is None or set(str(value)) == {_MASK}:
			return None
		return value

	def apply(self, **values):
		"""Set the given fields, remembering the site's own values the first time."""
		doc = frappe.get_doc(DOCTYPE)
		if not self._saved:
			passwords = _password_fields()
			self._saved = {f: self._read(doc, f, passwords) for f in self.fields}
		for field, value in values.items():
			if field not in self.fields:
				raise ValueError(
					f"{field!r} is not guarded — add it to SettingsGuard(...) or the "
					f"test value will be left behind on the live site")
			doc.set(field, value)
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache()

	def restore(self):
		"""Put the site's own values back. Safe to call when nothing was applied."""
		if not self._saved:
			return
		doc = frappe.get_doc(DOCTYPE)
		for field, value in self._saved.items():
			# None means "could not read it" (an unreadable secret) — leave whatever
			# is there rather than blanking a field we never successfully captured.
			if value is None:
				continue
			doc.set(field, value)
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache()
		self._saved = {}
