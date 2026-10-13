"""P4b slice 6 — ShipStation payload skips drop-ship lines.

A ``delivered_by_supplier`` line ships from RSR's dock, not ours. If the store's
ShipStation shipment carried it, we would print a parcel/packing-slip line for
goods that never enter the building — and on a MIXED counter order (a local gun we
DO ship + an RSR accessory we do not) the local parcel would list the RSR item.
The payload builder must drop those rows.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ffl_integrations.shipstation import api as ss

_dict = ss.frappe._dict


def _si(rows):
	return types.SimpleNamespace(
		name="SINV-1", customer="C", customer_name="Cust",
		ffl_destination_ffl=None, shipping_address_name=None,
		contact_email=None,
		items=[_dict(**r) for r in rows])


class ShipstationSkipTest(unittest.TestCase):
	def _build(self, rows):
		ps = [
			mock.patch.object(ss.frappe, "get_doc", lambda *a, **k: _si(rows)),
			mock.patch.object(ss, "serials_for_line", lambda row: []),
			mock.patch.object(ss.frappe.db, "get_single_value", lambda *a, **k: 16.0),
		]
		for p in ps:
			p.start()
			# Register each stop separately: unittest runs cleanups LIFO, which
			# unwinds overlapping patches correctly. Stopping them FIFO in one
			# cleanup restores a STUB as the 'original' when two patchers target
			# the same attribute — that leaked frappe.get_doc process-wide.
			self.addCleanup(p.stop)
		return ss.build_ship_to("SINV-1")

	def test_dropship_line_excluded_from_payload(self):
		env = self._build([
			{"item_code": "LOCAL", "item_name": "Local", "qty": 1,
				"delivered_by_supplier": 0},
			{"item_code": "RSR-ACC", "item_name": "RSR Acc", "qty": 1,
				"delivered_by_supplier": 1},
		])
		skus = [i["sku"] for i in env["items"]]
		self.assertIn("LOCAL", skus)
		self.assertNotIn("RSR-ACC", skus)  # RSR ships it — never in OUR parcel

	def test_pure_local_order_unchanged(self):
		env = self._build([
			{"item_code": "A", "item_name": "A", "qty": 1, "delivered_by_supplier": 0},
			{"item_code": "B", "item_name": "B", "qty": 2},  # field absent = local
		])
		self.assertEqual({i["sku"] for i in env["items"]}, {"A", "B"})

	def test_all_dropship_order_yields_no_parcel_items(self):
		env = self._build([
			{"item_code": "X", "item_name": "X", "qty": 1, "delivered_by_supplier": 1},
		])
		self.assertEqual(env["items"], [])

	def test_wholly_dropship_invoice_is_never_pushed(self):
		"""r1 n-2: with every line shipping from RSR there is nothing for the store to
		send — building/pushing an item-less parcel would create a phantom label."""
		empty = {"order_number": "SINV-1", "ship_to": {}, "ffl_number": None,
			"customer_email": None, "items": [], "serials": []}
		config = types.SimpleNamespace(
			default_parcel_weight_oz=16.0, ship_from_warehouse_id="WH-1")
		with mock.patch.object(ss, "build_ship_to", lambda si, default_oz=None: empty):
			with self.assertRaises(ss.frappe.ValidationError):
				ss.build_shipment("SINV-1", config=config)


if __name__ == "__main__":
	unittest.main()
