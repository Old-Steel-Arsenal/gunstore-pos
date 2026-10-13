"""Site-level install / uninstall of an extension, run on the long queue.

The bench already has the code (phase 0 put it there); these two jobs are the
same thing ``bench --site X install-app`` / ``uninstall-app`` do, driven from
the Extensions page. Progress is a field on the POS Extension row
(``job_state`` / ``job_message``) that the page polls — no RQ job lookup, and
a browser closed mid-way loses nothing.

Uninstall takes a database backup first (files are untouched by an app
removal) and then lets frappe remove the app's modules, DocTypes and tables;
the extension's own ``before_uninstall`` hook removes
what it added to core.
"""
from __future__ import annotations

import frappe
import frappe.utils
from frappe import _

from ffl_core.extensions import DOCTYPE, clear_enabled_cache
from ffl_core.utils import commit

# A row in one of these is owned by a job or by the host agent: no switch, no new
# job, no second upload for the id. The one definition — the page reads it too
# (api.extensions list_extensions → busy_states).
BUSY_STATES = ("Installing", "Staged", "Upgrading", "Uninstalling")
STATES = ("Idle", *BUSY_STATES, "Failed")        # = POS Extension.job_state options


def describe_error(e: BaseException) -> str:
	"""A sentence a store manager can read. ``str(e)`` is empty for anything
	raised through frappe.throw (the text lives in message_log), so fall back
	to the last message, then to the exception type."""
	text = str(e).strip()
	if not text and getattr(frappe.local, "message_log", None):
		last = frappe.local.message_log[-1]
		text = frappe.utils.strip_html(str(last.get("message") if isinstance(last, dict) else last)).strip()
	return (text or type(e).__name__)[:300]


def set_state(app: str, state: str, message: str = "") -> None:
	values = {"job_state": state, "job_message": (message or "")[:1000]}
	if state in BUSY_STATES:
		values["job_started_at"] = frappe.utils.now_datetime()
	frappe.db.set_value(DOCTYPE, app, values, update_modified=False)
	clear_enabled_cache()
	commit()


def _failed(app: str, verb: str, e: BaseException) -> None:
	"""Roll back, record why, and make the row describe reality again
	(remove_app commits in steps, so 'installed' may have flipped)."""
	from ffl_core.extensions.registry import sync_app

	frappe.db.rollback()
	frappe.log_error(title=f"extension {app}: {verb} failed")
	try:
		sync_app(app)
	except Exception:
		frappe.log_error(title=f"extension {app}: re-sync after failed {verb}")
	set_state(app, "Failed", _("{0} failed: {1}").format(verb, describe_error(e)))


def install_on_site(app: str) -> None:
	from frappe.installer import install_app
	from ffl_core.extensions.registry import sync_app

	set_state(app, "Installing")
	try:
		install_app(app)
		frappe.clear_cache()
		sync_app(app)
		set_state(app, "Idle")
	except Exception as e:
		_failed(app, _("Install"), e)
		raise


def uninstall_from_site(app: str) -> None:
	from frappe.installer import remove_app
	from frappe.utils.backups import new_backup
	from ffl_core.extensions.registry import sync_app
	from ffl_core.extensions.sideload import forget_rollback_point

	set_state(app, "Uninstalling")
	try:
		new_backup(ignore_files=True, force=True)
		remove_app(app, yes=True, no_backup=True)     # commits + clears cache itself
		sync_app(app)
		set_state(app, "Idle")
	except Exception as e:
		_failed(app, _("Uninstall"), e)
		raise
	try:
		forget_rollback_point(app)                    # an upgrade's way back means nothing once it is off the site
	except Exception:
		frappe.log_error(title=f"extension {app}: removing its upgrade rollback point failed")
