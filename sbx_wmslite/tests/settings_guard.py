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

import os

import frappe
from frappe.utils import add_to_date, now_datetime

DOCTYPE = "WMSLite Settings"
_MASK = "*"

# These suites change live settings for the length of a run. Restoring them at the
# end (below) fixes the AFTERMATH but not the WINDOW: while the suite runs, the
# site really is configured the way the tests want it.
#
# That is not theoretical. During one run an operator dispatched and received a
# coil at 18:12; the suite had `qr_decode_rule` on Identity and `rm_unknown_bin` on
# its test placeholder, so the scan was stored under the full QR string in a bin
# called __TEST__RM_UNKNOWN -- the exact defect the run was verifying the fix for.
# It had to be repaired by re-running v9 and v10.
#
# So: refuse to start if somebody is currently working. Set WMSLITE_ALLOW_LIVE_TESTS=1
# to override when you know the floor is idle. This is a safety net, not a
# solution -- the real answer is a test site that is not production.
LIVE_ACTIVITY_MINUTES = 15
OVERRIDE_ENV = "WMSLITE_ALLOW_LIVE_TESTS"


def _recent_operator_activity():
	"""Real in-app scans in the last few minutes — somebody is on the floor."""
	since = add_to_date(now_datetime(), minutes=-LIVE_ACTIVITY_MINUTES)
	scans = frappe.db.count("Coil Transaction", {
		"source": "In-app", "creation": (">", since),
		"coil_barcode": ("not like", "__TEST__%")})
	loads = frappe.db.count("Coil Load Event", {
		"creation": (">", since), "truck_number": ("not like", "__TEST__%")})
	return scans + loads


def assert_safe_to_mutate_settings():
	"""Abort the run rather than reconfigure the site under a working operator."""
	if os.environ.get(OVERRIDE_ENV):
		return
	busy = _recent_operator_activity()
	if busy:
		frappe.throw(
			f"Refusing to run: {busy} live scan(s) in the last {LIVE_ACTIVITY_MINUTES} "
			f"minutes. These tests reconfigure WMSLite Settings for the duration, so "
			f"anything scanned now would be recorded under test settings. Wait until "
			f"the floor is idle, or set {OVERRIDE_ENV}=1 if you know it is.")


def assert_no_live_pending_pushes():
	"""Refuse to run a suite that drives a mocked SAP sweep while REAL coils are
	queued for SAP.

	loading_push.sweep(), count_push.sweep() and rm_push.sweep() are site-wide: they push every
	Pending row, not just __TEST__ ones. Under a mocked `_http_post` that echoes
	"Data Pushed Successfully", a live Pending coil caught in the same batch is
	marked Sent although SAP never heard of it -- and nothing downstream can tell.

	That is not hypothetical. On 2026-08-31 a run marked 199 real coils on
	LP-00074..LP-00084 Sent this way; on 2026-09-23 coil 121493 the same, minutes
	after a supervisor had been watching it. Their WMSLite SAP Loading records
	carry the tell-tale: a response_excerpt containing "__TEST__".

	Deliberately NOT overridable by WMSLITE_ALLOW_LIVE_TESTS: that switch means
	"the floor is idle", which does nothing to make a queued coil safe. Push (or
	park) the live rows first, or run the suite on a site that is not production.
	"""
	loading = frappe.db.count("Loading Plan Coil", {
		"sap_loading_status": "Pending", "coil_barcode": ("not like", "__TEST__%")})
	count = frappe.db.count("Coil Transaction", {
		"sap_status": "Pending", "source": "In-app", "coil_barcode": ("not like", "__TEST__%")})
	if loading or count:
		frappe.throw(
			f"Refusing to run: {loading} live loading coil(s) and {count} live in-app scan(s) "
			f"are Pending for SAP. This suite mocks the SAP endpoint and its sweep is "
			f"site-wide, so those rows would be marked Sent without ever reaching SAP. "
			f"Let them push (or set them Not Required) first; there is no override.")


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
			assert_safe_to_mutate_settings()
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
