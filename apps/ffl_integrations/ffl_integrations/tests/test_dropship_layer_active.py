"""The Distributors hub opens the drop-ship layer's pages only when a layer is
there and switched on (dropship_hooks.layer_active)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from ffl_integrations.distributor import dropship_hooks


class LayerActive(unittest.TestCase):
	def _active(self, providers, extensions, on):
		with patch("frappe.get_hooks", return_value=providers), \
				patch("frappe.db.exists", side_effect=lambda dt, name: name in extensions), \
				patch("ffl_core.extensions.is_enabled", side_effect=lambda app: app in on):
			return dropship_hooks.layer_active()

	def test_no_provider_no_layer(self):
		self.assertFalse(self._active([], set(), set()))

	def test_a_switched_off_extension_is_no_layer(self):
		self.assertFalse(self._active(["acme_ship.listing.row"], {"acme_ship"}, set()))

	def test_a_switched_on_extension_is_the_layer(self):
		self.assertTrue(self._active(["acme_ship.listing.row"], {"acme_ship"}, {"acme_ship"}))

	def test_a_provider_that_is_not_an_extension_is_always_on(self):
		self.assertTrue(self._active(["some_app.listing.row"], set(), set()))
