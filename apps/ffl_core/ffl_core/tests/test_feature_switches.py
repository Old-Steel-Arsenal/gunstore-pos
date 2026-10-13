"""What a save of FFL Settings — the Single holding the store-level switches — refreshes.

(The switch that used to be tested here left with the dealer web store in 1.11.0; its
absence is pinned in test_remove_dealer_store.)
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.feature_gate import SETTINGS_DOCTYPE
from ffl_core.tests.settings_sandbox import SettingsSandbox


class SettingsSaveRefreshesOpenSessions(SettingsSandbox, FrappeTestCase):
	"""What a signed-in operator sees after somebody flips a switch.

	``sessions.get()`` serves an already-logged-in session out of
	``cache.hget("bootinfo", user)`` (frappe 16.22.0, sessions.py:137). The switch
	flags themselves are not in that blob's cached half — the ``extend_bootinfo``
	hooks run at sessions.py:167, outside the ``if not bootinfo`` block, so every
	boot recomputes them — but the rest of it is, ``allowed_workspaces`` included.
	A workspace save drops the hash (``Workspace.on_update``); a settings save did
	not, so the settings save drops it itself.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def test_saving_the_settings_drops_every_cached_bootinfo(self):
		# Not self.user: the switches are store-wide, so a colleague's stale blob
		# is exactly the one that has to go.
		other = "someone-else@example.test"
		frappe.cache.hset("bootinfo", other, {"stale": 1})
		self.assertIsNotNone(frappe.cache.hget("bootinfo", other))

		frappe.get_single(SETTINGS_DOCTYPE).save(ignore_permissions=True)

		self.assertIsNone(frappe.cache.hget("bootinfo", other))
