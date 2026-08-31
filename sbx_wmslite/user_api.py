"""WMS user administration for the console's Users view.

Lets a **WMS Manager** onboard warehouse users (create, assign the operational
roles, enable/disable, reset password) without going into the Frappe Desk.

SECURITY — this module writes User docs with ignore_permissions, so it is
effectively an admin API and is deliberately fenced in:

  * `ASSIGNABLE_ROLES` caps what may be granted. 'WMS Manager' is NOT in it —
    only a System Manager can mint new managers, so a manager can never escalate
    (their own role, nor System Manager, nor any ERP role).
  * `_assert_manageable()` restricts WHO may be edited: only users whose roles
    live entirely inside the WMS set. Administrator and any System Manager /
    ERP user is untouchable, so a manager can't reset a privileged password and
    take the site over. A manager also can't tamper with a peer WMS Manager.
  * No deletion — users are disabled (keeps audit links like Coil Load Event's
    operator / Loading Plan Coil's loaded_by intact).
  * Every mutation is written to the target user's timeline as a comment.

The site hosts other apps (erpnext, hrms, …), so an unfenced role-grant would be
a full-ERP compromise, not just a WMS one. Keep these guards intact.
"""

import frappe
from frappe import _
from frappe.utils import cint, validate_email_address

MANAGER = "WMS Manager"
# Roles a WMS Manager may grant/revoke. MANAGER is intentionally absent.
ASSIGNABLE_ROLES = ("Loading Operator", "Loading Supervisor", "Inventory Recorder")
# The full WMS role set — a user holding only these is "ours" (manageable here).
WMS_ROLES = ASSIGNABLE_ROLES + (MANAGER,)
# Implicit/benign Frappe roles that don't make a user "foreign".
_IGNORED_ROLES = {"All", "Guest", "Desk User"}
# Never editable through this portal, whatever roles they hold.
_PROTECTED_USERS = {"Administrator", "Guest"}


# --------------------------------------------------------------------------
# guards
# --------------------------------------------------------------------------
def _is_system_manager():
	return "System Manager" in frappe.get_roles()


def _require_user_admin():
	"""Only a WMS Manager (or System Manager) may administer users."""
	roles = set(frappe.get_roles())
	if not (roles & {MANAGER, "System Manager"}):
		frappe.throw(_("Not permitted — WMS Manager role required"), frappe.PermissionError)


def _allowed_roles():
	"""Roles the CURRENT caller may grant. Only a System Manager can add MANAGER."""
	return tuple(ASSIGNABLE_ROLES) + ((MANAGER,) if _is_system_manager() else ())


def _user_roles(user):
	return {r.role for r in frappe.get_all("Has Role", filters={"parent": user}, fields=["role"])}


def _foreign_roles(roles):
	"""Roles outside the WMS set — their presence means the user isn't ours."""
	return {r for r in roles if r not in WMS_ROLES and r not in _IGNORED_ROLES}


def _is_manageable(user, roles=None):
	"""True when this portal may edit `user` at all (before caller-level checks)."""
	if not user or user in _PROTECTED_USERS:
		return False
	roles = _user_roles(user) if roles is None else roles
	return not _foreign_roles(roles)


def _assert_manageable(user, for_write=True):
	"""Raise unless the caller may edit `user`. Returns the target's roles."""
	if not frappe.db.exists("User", user):
		frappe.throw(_("User {0} not found").format(user))
	if user in _PROTECTED_USERS:
		frappe.throw(_("{0} cannot be managed here").format(user), frappe.PermissionError)
	roles = _user_roles(user)
	foreign = _foreign_roles(roles)
	if foreign:
		# e.g. System Manager / ERP roles — out of scope for the WMS console.
		frappe.throw(_("{0} holds non-WMS roles ({1}) and must be managed in the Desk")
					 .format(user, ", ".join(sorted(foreign))), frappe.PermissionError)
	# A manager may not tamper with a peer manager (strip their role, reset their
	# password, disable them). Only a System Manager can.
	if MANAGER in roles and not _is_system_manager():
		frappe.throw(_("{0} is a WMS Manager — only a System Manager can change that account")
					 .format(user), frappe.PermissionError)
	if for_write and user == frappe.session.user:
		frappe.throw(_("You cannot change your own account here"), frappe.PermissionError)
	return roles


def _validate_roles(roles):
	"""Normalise + reject anything the caller may not grant."""
	if isinstance(roles, str):
		roles = frappe.parse_json(roles) if roles.strip().startswith("[") else [roles]
	roles = [str(r).strip() for r in (roles or []) if str(r).strip()]
	allowed = _allowed_roles()
	bad = [r for r in roles if r not in allowed]
	if bad:
		# The escalation stop: asking for System Manager (or MANAGER as a manager)
		# lands here and is refused outright.
		frappe.throw(_("These roles cannot be assigned here: {0}").format(", ".join(bad)),
					 frappe.PermissionError)
	return sorted(set(roles))


def _audit(user, message):
	"""Best-effort audit line on the user's timeline — never breaks the action."""
	try:
		frappe.get_doc("User", user).add_comment(
			"Info", f"[WMS Console] {message} — by {frappe.session.user}")
	except Exception:
		frappe.log_error(frappe.get_traceback(), "WMS user admin audit failed")


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------
@frappe.whitelist()
def list_wms_users():
	"""Every user holding a WMS role, with their roles and whether we may edit them."""
	_require_user_admin()
	names = {r.parent for r in frappe.get_all(
		"Has Role", filters={"role": ["in", list(WMS_ROLES)]}, fields=["parent"])}
	out = []
	if names:
		users = frappe.get_all(
			"User", filters={"name": ["in", list(names)]},
			fields=["name", "full_name", "first_name", "last_name", "enabled",
					"user_type", "mobile_no", "last_active"],
			order_by="full_name asc", limit_page_length=0)
		for u in users:
			roles = _user_roles(u.name)
			locked_reason = ""
			if u.name in _PROTECTED_USERS:
				locked_reason = "Protected account"
			elif _foreign_roles(roles):
				locked_reason = "Holds non-WMS roles — manage in Desk"
			elif MANAGER in roles and not _is_system_manager():
				locked_reason = "WMS Manager — System Manager only"
			elif u.name == frappe.session.user:
				locked_reason = "This is you"
			out.append({
				"name": u.name, "full_name": u.full_name or u.name,
				"enabled": cint(u.enabled), "mobile_no": u.mobile_no,
				"last_active": str(u.last_active) if u.last_active else None,
				"roles": sorted(r for r in roles if r in WMS_ROLES),
				"other_roles": sorted(_foreign_roles(roles)),
				"manageable": 1 if not locked_reason else 0,
				"locked_reason": locked_reason,
			})
	return {
		"users": out,
		"assignable_roles": list(_allowed_roles()),
		"is_system_manager": 1 if _is_system_manager() else 0,
	}


# --------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------
@frappe.whitelist()
def create_wms_user(user_id, first_name, roles, password, last_name=None, mobile_no=None):
	"""Create a warehouse user with the given WMS roles and an initial password.

	`user_id` is the login (Frappe uses an email-shaped id). Workers without real
	email can use a synthetic one, e.g. operator1@wms.local — no welcome email is
	sent, so nothing bounces.
	"""
	_require_user_admin()
	user_id = (user_id or "").strip().lower()
	first_name = (first_name or "").strip()
	if not user_id:
		frappe.throw(_("Login ID is required"))
	if not first_name:
		frappe.throw(_("First name is required"))
	validate_email_address(user_id, throw=True)
	# Role validation comes BEFORE the duplicate check on purpose: a request to
	# grant a forbidden role must be refused as an escalation attempt whether or
	# not the target happens to exist, so the fence never depends on site state.
	roles = _validate_roles(roles)
	if not roles:
		frappe.throw(_("Assign at least one role"))
	if frappe.db.exists("User", user_id):
		frappe.throw(_("A user with login {0} already exists").format(user_id))
	if not (password or "").strip():
		frappe.throw(_("An initial password is required"))

	doc = frappe.new_doc("User")
	doc.email = user_id
	doc.first_name = first_name
	doc.last_name = (last_name or "").strip() or None
	doc.mobile_no = (mobile_no or "").strip() or None
	doc.user_type = "System User"   # matches existing WMS users; role perms apply
	doc.enabled = 1
	doc.send_welcome_email = 0      # synthetic addresses must not be emailed
	doc.new_password = password     # Frappe hashes + applies the password policy
	for r in roles:
		doc.append("roles", {"role": r})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	_audit(doc.name, f"User created with roles: {', '.join(roles)}")
	return {"ok": True, "user": doc.name, "roles": roles}


@frappe.whitelist()
def set_user_roles(user, roles):
	"""Replace a WMS user's roles with `roles` (all validated against the cap)."""
	_require_user_admin()
	existing = _assert_manageable(user)
	roles = _validate_roles(roles)
	if not roles:
		frappe.throw(_("Assign at least one role — use Disable to revoke access"))
	doc = frappe.get_doc("User", user)
	doc.set("roles", [])
	for r in roles:
		doc.append("roles", {"role": r})
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	_audit(user, f"Roles changed: {', '.join(sorted(existing)) or 'none'} -> {', '.join(roles)}")
	return {"ok": True, "user": user, "roles": roles}


@frappe.whitelist()
def set_user_enabled(user, enabled):
	"""Enable or disable a WMS user (never delete — audit links must survive)."""
	_require_user_admin()
	_assert_manageable(user)
	enabled = 1 if cint(enabled) else 0
	frappe.db.set_value("User", user, "enabled", enabled)
	frappe.db.commit()
	_audit(user, "Account " + ("enabled" if enabled else "disabled"))
	return {"ok": True, "user": user, "enabled": enabled}


@frappe.whitelist()
def reset_user_password(user, password):
	"""Set a new password for a WMS user (manager-assisted reset for the floor)."""
	_require_user_admin()
	_assert_manageable(user)
	if not (password or "").strip():
		frappe.throw(_("A new password is required"))
	doc = frappe.get_doc("User", user)
	doc.new_password = password
	doc.save(ignore_permissions=True)   # Frappe hashes + enforces the policy
	frappe.db.commit()
	_audit(user, "Password reset")
	return {"ok": True, "user": user}
