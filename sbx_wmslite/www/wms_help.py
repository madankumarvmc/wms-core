import frappe


def get_context(context):
	# Public page — no login required (a shareable worker-facing manual).
	context.no_cache = 1
	context.no_header = 1
	context.no_breadcrumbs = 1
	return context
