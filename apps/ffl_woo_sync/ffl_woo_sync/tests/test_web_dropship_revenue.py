"""The woo-side revenue/fulfilment consequences of a drop-ship web order.

* ``build_invoice_items`` writes ``confirmed_serial`` onto the
  SI line unconditionally today. For an FDS gun that serial belongs to RSR's
  record, not to a Serial No we own — pinning it would drive ERPNext's serial
  machinery at a gun that is not in our stock. Those lines instead carry
  ``delivered_by_supplier`` and no serial.
* A purely-accessory web order never reaches ``Transferred`` through a
  disposition, so it never booked revenue at all. Auto-clear now books it.
* ``dispose_web_order`` handles the ``local`` and ``to_store`` units only.
  An FDS unit has no bound-book entry to exit from.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ffl_woo_sync.woocommerce.revenue import build_invoice_items


def _line(item_code="GLOCK-19", qty=1, line_total=639.0, woo_line_id="7"):
	return frappe._dict({"item_code": item_code, "qty": qty,
		"line_total": line_total, "unit_price": line_total, "woo_line_id": woo_line_id})


def _srow(item_code="GLOCK-19", confirmed_serial="SN-1", woo_line_id="7",
		unit_index=0, fulfillment="local"):
	return frappe._dict({"item_code": item_code, "confirmed_serial": confirmed_serial,
		"woo_line_id": woo_line_id, "unit_index": unit_index, "fulfillment": fulfillment})


def _order(lines, serials):
	o = MagicMock()
	o.name = "WOO-ORD-1"
	o.lines = lines
	o.serials = serials
	return o


def _no_dropship(*a, **k):
	"""Narrow stub: nothing is drop-ship-sourced unless a test says so."""
	return None


# ============================================ the SI line shape

# The drop-ship layer (osa_dropship) answers who ships an unrecorded line; here it
# says RSR ships ``item`` direct.
_SOURCE = "ffl_woo_sync.woocommerce.mapping.dropship_source"


def _rsr_ships(item):
	return lambda code, **kw: {"distributor": "RSR", "fulfillment_mode": "Direct"} if code == item else None


class TestDropshipInvoiceLines(unittest.TestCase):

	def test_fds_gun_line_is_delivered_by_supplier_and_unpinned(self):
		order = _order([_line("RSR-GLK19")],
			[_srow("RSR-GLK19", "RSRSERIAL1", fulfillment="fds")])
		with patch("frappe.db.get_value", side_effect=lambda dt=None, n=None, f=None,
				*a, **k: "RSR" if f == "dropship_distributor" else None):
			rows = build_invoice_items(order)
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row["delivered_by_supplier"], 1)
		self.assertEqual(row["dropship_distributor"], "RSR")
		self.assertNotIn("serial_no", row,
			"RSR's serial is a record, not one of our Serial No documents")
		self.assertEqual(row.get("warehouse", ""), "")

	def test_local_gun_line_still_pins_its_serial(self):
		"""REGRESSION: the ordinary web sale is unchanged."""
		order = _order([_line()], [_srow()])
		with patch("frappe.db.get_value", side_effect=_no_dropship):
			rows = build_invoice_items(order)
		self.assertEqual(rows[0]["serial_no"], "SN-1")
		self.assertNotIn("delivered_by_supplier", rows[0])

	def test_to_store_gun_line_is_an_ordinary_stock_line(self):
		"""Procure-to-store receives the gun into OUR inventory and disposes it locally, so its
		cost comes from the perpetual ledger. Marking it delivered_by_supplier
		would strip the COGS leg the receipt already created."""
		order = _order([_line("RSR-GLK19")],
			[_srow("RSR-GLK19", "OURS-9", fulfillment="to_store")])
		with patch("frappe.db.get_value", side_effect=lambda dt=None, n=None, f=None,
				*a, **k: "RSR" if f == "dropship_distributor" else None):
			rows = build_invoice_items(order)
		self.assertEqual(rows[0]["serial_no"], "OURS-9")
		self.assertNotIn("delivered_by_supplier", rows[0])

	def test_dropship_accessory_line_is_delivered_by_supplier(self):
		order = _order([_line("RSR-HOLOSUN", line_total=299.0)], [])
		with patch("frappe.db.get_value", side_effect=lambda dt=None, n=None, f=None,
				*a, **k: "RSR" if f == "dropship_distributor" else None), \
			 patch("frappe.db.has_column", return_value=True), \
			 patch(_SOURCE, side_effect=_rsr_ships("RSR-HOLOSUN")):
			rows = build_invoice_items(order)
		self.assertEqual(rows[0]["delivered_by_supplier"], 1)
		self.assertEqual(rows[0]["dropship_distributor"], "RSR")

	def test_local_accessory_line_is_untouched(self):
		order = _order([_line("AMMO-9MM", qty=2, line_total=50.0)], [])
		with patch("frappe.db.get_value", side_effect=_no_dropship):
			rows = build_invoice_items(order)
		self.assertEqual(rows[0], {"item_code": "AMMO-9MM", "qty": 2, "rate": 25.0})

	def test_a_flipped_source_does_not_rewrite_history(self):
		"""The lane RECORDED on the order at purchase time wins. If the Item is
		re-sourced to a distributor after a local gun was sold, that sale is still
		a stock sale and its serial must stay pinned."""
		order = _order([_line()], [_srow(fulfillment="local")])
		with patch("frappe.db.get_value", side_effect=lambda dt=None, n=None, f=None,
				*a, **k: "RSR" if f == "dropship_distributor" else None):
			rows = build_invoice_items(order)
		self.assertEqual(rows[0]["serial_no"], "SN-1")
		self.assertNotIn("delivered_by_supplier", rows[0])

	def test_mixed_order_splits_line_by_line(self):
		order = _order(
			[_line("GLOCK-19", woo_line_id="1"), _line("RSR-HOLOSUN", woo_line_id="2")],
			[_srow("GLOCK-19", "SN-1", woo_line_id="1", fulfillment="local")])

		def _gv(dt=None, n=None, f=None, *a, **k):
			if f == "dropship_distributor":
				return "RSR" if n == "RSR-HOLOSUN" else None
			return None

		with patch("frappe.db.get_value", side_effect=_gv), \
				patch(_SOURCE, side_effect=_rsr_ships("RSR-HOLOSUN")):
			rows = build_invoice_items(order)
		self.assertEqual(rows[0]["serial_no"], "SN-1")
		self.assertEqual(rows[1]["delivered_by_supplier"], 1)


# =========================================== dispose only the local units

class TestDisposeSkipsDistributorUnits(unittest.TestCase):

	def test_only_local_and_to_store_units_are_disposable(self):
		from ffl_woo_sync.woocommerce.fulfillment import _locally_disposed_rows

		rows = [
			_srow("A", "S-A", fulfillment="local"),
			_srow("B", "S-B", fulfillment="to_store"),
			_srow("C", "S-C", fulfillment="fds"),
			_srow("D", "S-D", fulfillment="direct"),
		]
		kept = _locally_disposed_rows(rows)
		self.assertEqual([r.item_code for r in kept], ["A", "B"])

	def test_a_blank_lane_counts_as_local(self):
		"""Every row written before this PR has no lane. Treating it as local
		preserves the existing behaviour exactly, and a mis-blanked drop-ship row
		fails LOUDLY (awaiting-serial) instead of silently skipping a real gun."""
		from ffl_woo_sync.woocommerce.fulfillment import _locally_disposed_rows

		rows = [frappe._dict({"item_code": "A", "confirmed_serial": "S-A"})]
		self.assertEqual(len(_locally_disposed_rows(rows)), 1)

	def test_an_fds_unit_without_a_serial_does_not_block_the_local_ones(self):
		"""The awaiting-serial refusal exists so we never half-ship a bound-book
		order. An FDS unit has no serial of ours to wait for."""
		from ffl_woo_sync.woocommerce.fulfillment import _locally_disposed_rows

		rows = [
			_srow("A", "S-A", fulfillment="local"),
			_srow("C", "", fulfillment="fds"),
		]
		kept = _locally_disposed_rows(rows)
		self.assertEqual(len(kept), 1)
		self.assertTrue(all((r.confirmed_serial or "").strip() for r in kept))


if __name__ == "__main__":
	unittest.main()
