"""0.1.0 restarts the version line below the private 1.15.x one, so What's New would stay silent.

Frappe's login popup lists the notes between ``User.last_known_versions[app].version`` and the
running version (``frappe.utils.change_log.get_change_log``); a user last seen on 1.15.1 gets
nothing for 0.1.0, and the stored 1.15.1 is never advanced, so no note would pop until a release
passed 1.15.1. For the POS's three apps, a stored version above the running one goes back to
"0.0.1" (frappe's own default for an app it has not seen): the 0.1.0 note pops once, closing it
stores 0.1.0, and later releases pop as usual. Other apps' entries and versions at or below the
running one are left alone; a value that does not parse is left alone. Idempotent.

Cosmetic only: no document, ledger or stock row is touched. On a rollback to 1.15.1 the popup
shows the 1.x notes once, nothing else.
"""
import json

import frappe
from semantic_version import Version

APPS = ("ffl_core", "ffl_integrations", "ffl_woo_sync")
NEVER_SEEN = "0.0.1"


def _above(stored, current: Version) -> bool:
	try:
		return Version(str(stored)) > current
	except ValueError:
		return False


def reset(known: dict, current: Version) -> bool:
	"""Reset the POS apps' versions above ``current`` in place; True when anything changed."""
	changed = False
	for app in APPS:
		entry = known.get(app)
		if isinstance(entry, dict) and _above(entry.get("version"), current):
			entry["version"] = NEVER_SEEN
			changed = True
	return changed


def execute():
	from ffl_core import __version__

	current = Version(__version__)
	for user in frappe.get_all("User", filters={"last_known_versions": ("is", "set")},
			fields=["name", "last_known_versions"]):
		try:
			known = json.loads(user.last_known_versions)
		except ValueError:
			continue
		if isinstance(known, dict) and reset(known, current):
			frappe.db.set_value("User", user.name, "last_known_versions", json.dumps(known), update_modified=False)
