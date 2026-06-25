import frappe


def get_context(context):
	# Require login — redirect guests to the login page and back.
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/wms-loading"
		raise frappe.Redirect

	context.no_cache = 1
	context.no_header = 1
	context.no_breadcrumbs = 1
	context.user = frappe.session.user
	context.full_name = frappe.utils.get_fullname(frappe.session.user)
	context.csrf_token = frappe.sessions.get_csrf_token()
	return context
