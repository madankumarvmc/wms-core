"""HHT task registry — which tasks a user may perform, and the shared guard.

The handheld hosts several tasks in one PWA (Loading, Inventory Count, RM Gate
Dispatch, RM Line Receipt, Inventory lookup). Each is gated by a role set and, except Loading, by a
feature flag in WMSLite Settings. Keeping that in one table means adding the next
task costs a dict entry rather than another branch in an if/elif chain — the
`my_tasks` endpoint, the role guards and the PWA's task picker all read from here.

`Loading Supervisor` and `System Manager` are superusers: they appear in every
role set and so see every enabled task.

The whitelisted endpoint stays at `sbx_wmslite.count_api.my_tasks` (which delegates
here). A handheld running a cached app shell calls that path, so it must keep
working even though the logic has moved.
"""

import frappe
from frappe import _
from frappe.utils import cint

# Suffix on every message describing a failure that can NEVER succeed on retry.
# `api._terminal_failure` matches this one marker, so a queued offline event that is
# permanently doomed is surfaced as dismissable instead of retried forever. A single
# sentinel beats matching English prose, which breaks the moment a string is
# translated.
#
# It lives here rather than in rm_api because `locations` needs it too, and rm_api
# imports locations — the same reason bin_api holds RM_IN_TRANSIT. rm_api re-exports
# it, so `rm_api.FINAL` still resolves.
FINAL = "[rm-final]"


def throw_final(msg):
	"""Throw a permanent failure — one an offline replay must give up on."""
	frappe.throw(f"{msg} {FINAL}")


SUPERUSERS = {"Loading Supervisor", "System Manager"}

LOADING_ROLES = {"Loading Operator"} | SUPERUSERS
COUNT_ROLES = {"Inventory Recorder"} | SUPERUSERS
RM_GATE_ROLES = {"RM Gate Operator"} | SUPERUSERS
RM_LINE_ROLES = {"RM Line Operator"} | SUPERUSERS
# Inventory lookup is read-only, so every handheld role gets it (owner, 2026-10-08).
INVENTORY_ROLES = LOADING_ROLES | COUNT_ROLES | RM_GATE_ROLES | RM_LINE_ROLES

# key -> (roles that may perform it, WMSLite Settings flag or None). Order = tile order.
TASKS = (
	{"key": "loading", "roles": LOADING_ROLES, "flag": None},
	{"key": "count", "roles": COUNT_ROLES, "flag": "count_enabled"},
	{"key": "rm_dispatch", "roles": RM_GATE_ROLES, "flag": "rm_enabled"},
	{"key": "rm_receive", "roles": RM_LINE_ROLES, "flag": "rm_enabled"},
	{"key": "inventory", "roles": INVENTORY_ROLES, "flag": "inventory_view_enabled"},
)


def flag_on(flag):
	"""Read a feature flag fresh, NOT via the cached Single.

	A cached Single can serve a stale value and mask a flag an admin just changed
	(the same trap documented for the outbound-SAP settings).
	"""
	if not flag:
		return True
	return bool(cint(frappe.db.get_single_value("WMSLite Settings", flag)))


def my_tasks():
	"""The task keys the current user may perform — drives the PWA task picker."""
	roles = set(frappe.get_roles())
	tasks = [t["key"] for t in TASKS if (roles & t["roles"]) and flag_on(t["flag"])]
	return {"tasks": tasks, "default": None}


def require(roles, flag=None, feature=None):
	"""Assert the caller may perform a task. Call FIRST in every endpoint.

	UI gating is a convenience, not a security boundary — the handheld hides tasks
	the user lacks, but the endpoints are what actually enforce it.

	The PermissionError is deliberate: `api._terminal_failure` treats it as a
	permanent failure, so a queued offline event from a user whose role was revoked
	is dropped rather than retried forever.
	"""
	if not (set(frappe.get_roles()) & roles):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if flag and not flag_on(flag):
		frappe.throw(_("{0} is disabled").format(feature or _("This task")))
