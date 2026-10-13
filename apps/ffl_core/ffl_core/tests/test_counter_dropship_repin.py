"""P4b slice 2 — drop-ship re-pin in ``_repin_economics`` / ``_apply_serials``.

``set_missing_values`` / ``set_pos_fields`` re-derive line economics on every save,
so create_order re-pins after each. A drop-ship line must come back as ERPNext's
native drop-ship row — ``delivered_by_supplier=1``, no warehouse, the distributor
pinned — and it must NEVER be serial-pinned (there is no local serial). A local
firearm on the same order still gets its serial pinned (regression).
"""
from __future__ import annotations

import types
import unittest

from ffl_core.api import manual_order

_dict = manual_order.frappe._dict


def _si(items):
	# The SI is a plain namespace: ``_dict.items`` would collide with dict.items().
	# Rows are _dict (attr get/set + .get(), like a real SI Item doc row).
	return types.SimpleNamespace(items=[_dict(**it) for it in items])


class RepinDropshipTest(unittest.TestCase):
	def test_dropship_row_becomes_delivered_by_supplier_no_warehouse(self):
		si = _si([{"item_code": "RSR-ACC", "rate": 0, "warehouse": "Store - X"}])
		rows = [{"item_code": "RSR-ACC", "qty": 2, "rate": 30, "serials": [],
			"source": "dropship", "warehouse": "", "dropship_distributor": "RSR"}]
		manual_order._repin_economics(si, rows)
		row = si.items[0]
		self.assertEqual(row.delivered_by_supplier, 1)
		self.assertFalse(row.warehouse)  # cleared — RSR ships, not our shelf
		self.assertEqual(row.dropship_distributor, "RSR")
		self.assertEqual(row.rate, 30)             # staff rate still re-pinned
		self.assertFalse(row.get("serial_no"))     # never serial-pinned

	def test_dropship_gun_row_drops_a_fifo_picked_shelf_serial(self):
		"""ERPNext's item-details pass hands a stock gun row an in-stock serial while
		the invoice still reads update_stock=1 — another customer's gun. The
		distributor ships this one, so the re-pin clears it (as for ship to store)."""
		si = _si([{"item_code": "RSR-GUN", "rate": 0, "warehouse": "Store - X",
			"serial_no": "SHELF-1"}])
		rows = [{"item_code": "RSR-GUN", "qty": 1, "rate": 900, "serials": [],
			"source": "dropship", "warehouse": "", "dropship_distributor": "RSR"}]
		manual_order._repin_economics(si, rows)
		self.assertFalse(si.items[0].serial_no)

	def test_local_firearm_still_serial_pinned(self):
		si = _si([{"item_code": "LOCAL-GUN", "rate": 0}])
		rows = [{"item_code": "LOCAL-GUN", "qty": 1, "rate": 500,
			"serials": ["SN123"], "warehouse": "Vault - X"}]
		manual_order._repin_economics(si, rows)
		row = si.items[0]
		self.assertEqual(row.serial_no, "SN123")
		self.assertEqual(row.warehouse, "Vault - X")
		self.assertEqual(row.use_serial_batch_fields, 1)
		self.assertIsNone(row.get("delivered_by_supplier"))

	def test_mixed_order_repins_each_row_its_own_way(self):
		si = _si([{"item_code": "LOCAL-GUN"}, {"item_code": "RSR-ACC"}])
		rows = [
			{"item_code": "LOCAL-GUN", "qty": 1, "rate": 500, "serials": ["SN1"],
				"warehouse": "Vault - X"},
			{"item_code": "RSR-ACC", "qty": 1, "rate": 20, "serials": [],
				"source": "dropship", "warehouse": "", "dropship_distributor": "RSR"},
		]
		manual_order._repin_economics(si, rows)
		self.assertEqual(si.items[0].serial_no, "SN1")
		self.assertIsNone(si.items[0].get("delivered_by_supplier"))
		self.assertEqual(si.items[1].delivered_by_supplier, 1)
		self.assertFalse(si.items[1].get("serial_no"))

	def test_apply_serials_skips_dropship_row(self):
		si = _si([{"item_code": "RSR-GUN"}])
		rows = [{"item_code": "RSR-GUN", "qty": 1, "rate": 500, "serials": [],
			"source": "dropship", "warehouse": ""}]
		manual_order._apply_serials(si, rows)
		self.assertIsNone(si.items[0].get("serial_no"))


if __name__ == "__main__":
	unittest.main()
