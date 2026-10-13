"""POS Extension — the registry row and the store's on/off switch for one extension.

Rows are created by ffl_core.extensions.registry.sync (never by hand:
``in_create``); the only field a person edits is ``enabled`` — the store's
intent, which nothing else writes. Saving invalidates
frappe's document cache (which is what ``is_enabled`` reads) and drops the
desk boot cache, so the next request — hook, endpoint or page load — sees the
new state.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from ffl_core.extensions import clear_enabled_cache, effective


class POSExtension(Document):
	def validate(self):
		# Only turning it ON is refused. A row may stay on while incompatible or not
		# installed here — the registry marks those facts and effective() honours them.
		if self.is_new() or not self.enabled or not self.has_value_changed("enabled"):
			return
		if not self.compatible:
			frappe.throw(
				_("{0} cannot be turned on: {1}.").format(self.title or self.name, self.incompatible_reason or _("not compatible")),
				title=_("Not compatible"),
			)
		if not self.installed_on_site:
			frappe.throw(_("{0} cannot be turned on: it is not installed on this site.").format(self.title or self.name),
				title=_("Not installed"))

	def on_update(self):
		# The row itself is in frappe's document cache (invalidated by this
		# save); the desk boot cache carries gunshop_extensions and is not.
		clear_enabled_cache()
		before = self.get_doc_before_save()
		# Installed before and after: install / uninstall are the extension's own
		# after_install / before_uninstall moments, not toggles.
		if before is not None and before.installed_on_site and self.installed_on_site and effective(before) != effective(self):
			notify_toggled(self.name, effective(self))

	def on_trash(self):
		clear_enabled_cache()


def notify_toggled(app: str, enabled: bool) -> None:
	"""hooks ``gunshop_extension_toggled``: every provider gets ``(app, effective)``
	once each time the switch as it acts flips — the store's switch or a core
	compatibility change, never an install or uninstall — on purpose NOT through @extension_hook,
	because the OFF flip is the one an extension needs most (to take its
	workspace shortcuts down). A provider that fails is logged; the switch
	itself is already saved."""
	for hook in frappe.get_hooks("gunshop_extension_toggled"):
		try:
			frappe.get_attr(hook)(app, enabled)
		except Exception:
			frappe.log_error(title=f"gunshop_extension_toggled failed: {hook}")
