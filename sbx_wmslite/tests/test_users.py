"""Tests for WMS user administration (console Users view).

The point of these tests is the SECURITY FENCE, not the CRUD: a WMS Manager must
never be able to escalate (grant System Manager or mint another manager), and must
never be able to touch a privileged / non-WMS account.

__TEST__-namespaced users are created and removed. Run:
    bench --site <site> run-tests --app sbx_wmslite --module sbx_wmslite.tests.test_users
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from sbx_wmslite import user_api

MGR = "__test__wmsmgr@wms.local"
OPER = "__test__wmsoper@wms.local"
FOREIGN = "__test__wmsforeign@wms.local"
# Scratch id several tests create/destroy — must never leak between tests, or a
# leftover trips the duplicate check before the guard under test is reached.
NEW = "__test__new@wms.local"
PWD = "WmsT3st!Pass99"
# A benign non-WMS role — proves the "foreign roles" guard without ever minting a
# System Manager on a live site.
FOREIGN_ROLE = "Blogger"


def _mkuser(email, roles, first="Test"):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=1, ignore_permissions=True)
	d = frappe.new_doc("User")
	d.email = email
	d.first_name = first
	d.user_type = "System User"
	d.send_welcome_email = 0
	d.new_password = PWD
	for r in roles:
		d.append("roles", {"role": r})
	d.insert(ignore_permissions=True)
	frappe.db.commit()
	return d.name


def _drop(*emails):
	for e in emails:
		if frappe.db.exists("User", e):
			frappe.delete_doc("User", e, force=1, ignore_permissions=True)
	frappe.db.commit()


def _cleanup():
	frappe.set_user("Administrator")
	_drop(MGR, OPER, FOREIGN, NEW)


class TestWmsUserAdmin(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_cleanup()
		_mkuser(MGR, [user_api.MANAGER], first="Mgr")
		_mkuser(OPER, ["Loading Operator"], first="Oper")
		_mkuser(FOREIGN, ["Loading Operator", FOREIGN_ROLE], first="Foreign")

	@classmethod
	def tearDownClass(cls):
		_cleanup()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		_drop(NEW)             # every test starts with the scratch id free
		frappe.set_user(MGR)   # act as a WMS Manager unless a test says otherwise

	def tearDown(self):
		frappe.set_user("Administrator")
		_drop(NEW)

	# ---- access ----
	def test_non_admin_is_refused(self):
		frappe.set_user(OPER)   # plain operator
		with self.assertRaises(frappe.PermissionError):
			user_api.list_wms_users()
		with self.assertRaises(frappe.PermissionError):
			user_api.create_wms_user(NEW, "X", ["Loading Operator"], PWD)

	def test_manager_can_list(self):
		d = user_api.list_wms_users()
		names = [u["name"] for u in d["users"]]
		self.assertIn(OPER, names)
		# a manager may NOT grant the manager role
		self.assertNotIn(user_api.MANAGER, d["assignable_roles"])
		self.assertEqual(d["is_system_manager"], 0)

	# ---- escalation fence ----
	def test_cannot_grant_system_manager(self):
		with self.assertRaises(frappe.PermissionError):
			user_api.create_wms_user(NEW, "X", ["System Manager"], PWD)
		with self.assertRaises(frappe.PermissionError):
			user_api.set_user_roles(OPER, ["Loading Operator", "System Manager"])
		# unchanged
		self.assertNotIn("System Manager", user_api._user_roles(OPER))

	def test_manager_cannot_mint_another_manager(self):
		with self.assertRaises(frappe.PermissionError):
			user_api.set_user_roles(OPER, [user_api.MANAGER])
		with self.assertRaises(frappe.PermissionError):
			user_api.create_wms_user(NEW, "X", [user_api.MANAGER], PWD)

	def test_system_manager_can_grant_manager(self):
		frappe.set_user("Administrator")
		d = user_api.list_wms_users()
		self.assertIn(user_api.MANAGER, d["assignable_roles"])
		user_api.set_user_roles(OPER, ["Loading Operator", user_api.MANAGER])
		self.assertIn(user_api.MANAGER, user_api._user_roles(OPER))
		user_api.set_user_roles(OPER, ["Loading Operator"])   # restore

	# ---- who may be edited ----
	def test_cannot_touch_foreign_role_user(self):
		with self.assertRaises(frappe.PermissionError):
			user_api.set_user_roles(FOREIGN, ["Loading Operator"])
		with self.assertRaises(frappe.PermissionError):
			user_api.reset_user_password(FOREIGN, PWD)
		with self.assertRaises(frappe.PermissionError):
			user_api.set_user_enabled(FOREIGN, 0)

	def test_cannot_touch_administrator(self):
		for fn in (lambda: user_api.reset_user_password("Administrator", PWD),
				   lambda: user_api.set_user_enabled("Administrator", 0),
				   lambda: user_api.set_user_roles("Administrator", ["Loading Operator"])):
			with self.assertRaises(frappe.PermissionError):
				fn()

	def test_cannot_change_own_account(self):
		with self.assertRaises(frappe.PermissionError):
			user_api.set_user_enabled(MGR, 0)
		with self.assertRaises(frappe.PermissionError):
			user_api.set_user_roles(MGR, ["Loading Operator"])

	def test_manager_cannot_touch_peer_manager(self):
		frappe.set_user("Administrator")
		peer = _mkuser(NEW, [user_api.MANAGER], first="Peer")
		frappe.set_user(MGR)
		with self.assertRaises(frappe.PermissionError):
			user_api.set_user_enabled(peer, 0)
		with self.assertRaises(frappe.PermissionError):
			user_api.reset_user_password(peer, PWD)

	# ---- happy path ----
	def test_create_then_change_roles_and_disable(self):
		r = user_api.create_wms_user(NEW, "New", ["Loading Operator"], PWD,
									 last_name="Worker", mobile_no="9999999999")
		self.assertTrue(r["ok"])
		u = r["user"]
		self.assertEqual(frappe.db.get_value("User", u, "user_type"), "System User")
		self.assertEqual(user_api._user_roles(u), {"Loading Operator"})

		user_api.set_user_roles(u, ["Loading Operator", "Inventory Recorder"])
		self.assertEqual(user_api._user_roles(u), {"Loading Operator", "Inventory Recorder"})

		user_api.set_user_enabled(u, 0)
		self.assertEqual(frappe.db.get_value("User", u, "enabled"), 0)
		user_api.set_user_enabled(u, 1)
		self.assertEqual(frappe.db.get_value("User", u, "enabled"), 1)

		user_api.reset_user_password(u, "An0ther!Pass77")   # must not raise

	def test_duplicate_and_validation(self):
		with self.assertRaises(frappe.ValidationError):
			user_api.create_wms_user(OPER, "Dup", ["Loading Operator"], PWD)   # exists
		with self.assertRaises(frappe.ValidationError):
			user_api.create_wms_user(NEW, "X", [], PWD)    # no roles
		with self.assertRaises(frappe.ValidationError):
			user_api.create_wms_user(NEW, "", ["Loading Operator"], PWD)  # no name
