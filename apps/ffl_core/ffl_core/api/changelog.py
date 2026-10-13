"""What's New — the product version and every release note, for the Help menu.

Frappe already ships a change-log mechanism (`frappe.utils.change_log`): one
markdown file per release under `<app>/change_log/v<major>/v<x_y_z>.md`,
a per-user diff against `User.last_known_versions`, and a one-time popup on
login. Two gaps for a shop floor: the popup only fires for System Manager
(desk.js `show_notices`), and there is no way to read old notes again. The
bundle (`ffl_core.bundle.js`) fires the native popup for everyone; this
endpoint backs the sidebar-header menu → "What's New in POS" dialog (hooks
`standard_navbar_items`) that lists every note.

Only ffl_core carries a change_log folder — the three apps share one product
version (`ffl_core.__version__`, mirrored by the other two; see
`scripts/bump_version.py`). A pre-release has its own note named with its
tail (`1.0.0-beta.1` → `v1_0_0-beta_1.md`): frappe means to strip the tail
before matching, but `to_version.prerelease = None` does not change a
semantic_version 2.10 comparison, so only an exact name matches.
"""
from __future__ import annotations

import frappe
from frappe.utils.change_log import get_change_log_for_app

APP = "ffl_core"
_BEFORE_EVERYTHING = "0.0.0"


def read_release_notes(app: str = APP, version: str | None = None) -> list[list[str]]:
	"""Every release note on disk as `[version, markdown]`, newest first —
	frappe's own reader, opened to the whole history. ``app`` / ``version``
	let the Extensions page read an extension's change_log the same way
	(same pre-release file-name rule, see the module docstring)."""
	if version is None:
		from ffl_core import __version__
		version = __version__

	return get_change_log_for_app(app, from_version=_BEFORE_EVERYTHING, to_version=version) or []


@frappe.whitelist()
def get_changelog():
	"""The current POS version and all release notes, newest first.

	Backs the sidebar-header menu → "What's New in POS" dialog (`gunstore.showWhatsNew()`).
	Any logged-in desk user may call it: the notes are static files shipped
	with the code, nothing store-specific.

	Returns:
	    dict: `{"version": "1.0.0-beta.1", "entries": [["1.0.0-beta.1", "<markdown>"], …]}`
	    — `entries` sorted by semantic version, newest first.
	"""
	from ffl_core import __version__

	return {"version": __version__, "entries": read_release_notes()}
