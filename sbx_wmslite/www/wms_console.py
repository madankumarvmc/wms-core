import frappe


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/wms-console"
		raise frappe.Redirect

	roles = set(frappe.get_roles())
	# WMS Manager gets full console access (plus the Users view, which its own
	# endpoints gate independently — see user_api).
	if not (roles & {"Loading Supervisor", "WMS Manager", "System Manager"}):
		raise frappe.PermissionError("Loading Supervisor role required")

	context.no_cache = 1
	context.no_header = 1
	context.no_breadcrumbs = 1
	context.user = frappe.session.user
	context.full_name = frappe.utils.get_fullname(frappe.session.user)
	context.csrf_token = frappe.sessions.get_csrf_token()
	# Drives whether the Users nav item renders (endpoints re-check server-side).
	context.can_manage_users = 1 if (roles & {"WMS Manager", "System Manager"}) else 0
	return context
