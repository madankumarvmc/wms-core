"""Create the WMSLite roles and grant their permissions.

Two roles:
  * 'Loading Operator'   — field/bay users: look up trucks + scan/confirm coils.
  * 'Loading Supervisor' — back-office console users: import, approve short-loads,
    cancel/reopen plans, manage the doctypes (NOT full System Manager).

Run:
    bench --site <site> console
    >>> import sbx_wmslite.setup_roles as r; r.run()

With developer_mode on, re-saving the DocTypes exports the new permission
rows into the app JSON so they ship with the app.
"""

import frappe

OPERATOR = "Loading Operator"
SUPERVISOR = "Loading Supervisor"

# Full manage rights for the console — but not System Manager.
_MANAGE = {"read": 1, "write": 1, "create": 1, "delete": 1, "export": 1, "report": 1, "print": 1}

# role -> {doctype -> permission flags}.  Doctypes are guarded with exists()
# so this stays runnable before migrate.
ROLE_PERMS = {
	OPERATOR: {
		"Loading Plan": {"read": 1, "write": 1},
		"Coil Load Event": {"read": 1, "write": 1, "create": 1},
		"Bin Inventory": {"read": 1, "write": 1},
		"WMSLite Settings": {"read": 1},
	},
	SUPERVISOR: {
		"Loading Plan": _MANAGE,
		"Coil Load Event": _MANAGE,
		"Import Batch": _MANAGE,
		"WMSLite SAP Confirmation": _MANAGE,
		"Bin Inventory": _MANAGE,
		"WMSLite Settings": {"read": 1, "write": 1},
	},
}


def _ensure_role(role):
	if not frappe.db.exists("Role", role):
		frappe.get_doc({
			"doctype": "Role", "role_name": role,
			"desk_access": 1, "two_factor_auth": 0,
		}).insert(ignore_permissions=True)
		print("Created role:", role)
	else:
		print("Role exists:", role)


def _ensure_perm(dt, role, flags):
	"""Grant `flags` to `role` on `dt` via the runtime permissions API.

	We deliberately avoid re-saving the DocType (which triggers route-conflict
	validation in production). add_permission() clones the existing standard
	perms into Custom DocPerm on first touch, so System Manager rights are
	preserved."""
	from frappe.permissions import add_permission, update_permission_property

	if not frappe.db.exists("DocType", dt):
		print("skip (doctype missing):", dt)
		return
	add_permission(dt, role, 0)
	for ptype, val in flags.items():
		update_permission_property(dt, role, 0, ptype, val, validate=False)
	print("ensured perm:", role, "on", dt, flags)


def run():
	for role, perms in ROLE_PERMS.items():
		_ensure_role(role)
		for dt, flags in perms.items():
			_ensure_perm(dt, role, flags)
	frappe.db.commit()
	print("Done.")
