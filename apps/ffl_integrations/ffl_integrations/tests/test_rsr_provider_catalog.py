"""P3 — RSR provider catalog surface used by the listing pipeline, for the
OPTIONAL provider method the Task 3 remote-catalog rewire did not touch:
`promote_to_item` (delegates to `rsr/promote.py`, untouched).

Catalog reads (`search`, `catalog_row`, `feed_heartbeat`,
`catalog_content_heartbeat`, `prefetch_rows`) and `blocked_states` go through
the remote catalog stub — see
`test_rsr_provider_remote.py`.

Frappe is mocked at the boundary — no site, no HTTP.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ffl_integrations.distributor import rsr_provider


class TestPromoteDelegation(unittest.TestCase):
	def test_promote_delegates_with_sku_and_uniform_sell_price(self):
		import sys
		import types as _types

		promote_mod = _types.ModuleType("ffl_integrations.rsr.promote")
		seen = {}

		def _promote_to_item(payload):
			seen.update(payload)
			return {"item_code": "GLK19", "uniform_sell_price": True}

		promote_mod._promote_to_item = _promote_to_item
		with mock.patch.dict(sys.modules, {"ffl_integrations.rsr.promote": promote_mod}):
			out = rsr_provider.RSRProvider().promote_to_item("GLK19", {"uniform_sell_price": 1})

		self.assertEqual(out["item_code"], "GLK19")
		self.assertEqual(seen["rsr_stock_number"], "GLK19")
		self.assertEqual(seen["uniform_sell_price"], 1)

	def test_item_group_override_is_passed_through(self):
		import sys
		import types as _types

		promote_mod = _types.ModuleType("ffl_integrations.rsr.promote")
		seen = {}

		def _promote_to_item(payload):
			seen.update(payload)
			return {"item_code": "X"}

		promote_mod._promote_to_item = _promote_to_item
		with mock.patch.dict(sys.modules, {"ffl_integrations.rsr.promote": promote_mod}):
			rsr_provider.RSRProvider().promote_to_item("X", {"item_group": "Pistol"})
		self.assertEqual(seen["item_group"], "Pistol")


if __name__ == "__main__":
	unittest.main()
