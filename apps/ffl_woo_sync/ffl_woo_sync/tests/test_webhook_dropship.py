"""The webhook half of web drop-ship fulfilment.

Three behaviours, each of which is a correctness rule rather than a preference:

* A drop-shipped firearm line gets an EMPTY ``serials`` row carrying
  its lane, and reserves NOTHING. Reserving would hold one of our own guns
  against an order that will be filled from RSR's warehouse — the "poisoned
  order" failure. FDS serials that come back from RSR are recorded, never
  booked: no local Serial No, no disposition, no stock move.
* ``_auto_clear_non_firearm_if_completed`` must stand down while a
  distributor order is still outstanding, or an accessory order marks itself
  Transferred (and books revenue) before the goods have shipped.
* The cancel path writes with ``db.set_value``, which fires no
  doc_events — so it must call the ``woo_order_cancelled`` hook explicitly, or a
  cancelled order's un-placed DO is never withdrawn.

Stub discipline: no bare ``return_value`` on a frappe primitive. Each
stub answers only the doctype/field under test and returns None (= "not my
question") for everything else, so frappe's own reads underneath keep working.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ffl_woo_sync.woocommerce.webhook import (
	_auto_clear_non_firearm_if_completed,
	_fulfillment_lane,
	_handle_order_cancelled,
	_process_lines,
)


# --------------------------------------------------------------------- helpers

class _Order:
	"""Minimal stand-in for the Woo Online Order document `_process_lines` fills."""

	def __init__(self):
		self.lines = []
		self.serials = []
		self.dropship_status = None

	def append(self, table, row):
		getattr(self, table).append(frappe._dict(row))

	def get(self, key, default=None):
		return getattr(self, key, default)


def _gv_factory(*, firearm_items=()):
	"""A NARROW `frappe.db.get_value` stub.

	Answers exactly one question — "is this Item a regulated firearm?" — and
	returns None for everything else so any other read (including frappe's own,
	underneath) behaves as unstubbed.
	"""

	def _gv(doctype=None, name=None, field=None, *a, **k):
		if doctype == "Item" and isinstance(field, (list, tuple)) and "is_firearm" in field:
			return frappe._dict({
				"is_firearm": 1 if name in firearm_items else 0,
				"item_group": "Pistol" if name in firearm_items else "Accessories",
				"is_antique": 0,
			})
		return None

	return _gv


def _source(dropship_items=None, modes=None):
	"""ffl_integrations' ``dropship_source_for_item`` answer — the only thing the
	webhook asks about who ships an Item (``mapping.dropship_source``)."""
	dropship_items, modes = dropship_items or {}, modes or {}

	def _src(item_code, lead=True):
		if not dropship_items.get(item_code):
			return None
		return {"distributor": dropship_items[item_code], "sku": f"SKU-{item_code}",
			"fulfillment_mode": modes.get(item_code) or "", "lead_days": ""}

	return patch("ffl_woo_sync.woocommerce.mapping.dropship_source", side_effect=_src)


def _line(sku, qty=1, line_id="10"):
	return {"id": line_id, "sku": sku, "quantity": qty, "price": 100,
		"subtotal": 100, "total": 100, "total_tax": 0}


# ==================================================== no serial hold

class TestDropshipLinesReserveNothing(unittest.TestCase):

	def _run(self, lines, *, firearm_items=(), dropship_items=None, modes=None):
		order = _Order()
		_gv = _gv_factory(firearm_items=firearm_items)

		with patch("frappe.db.get_value", side_effect=_gv), \
			 _source(dropship_items, modes), \
			 patch("ffl_woo_sync.woocommerce.webhook._pick_oldest_unreserved_serial") as pick:
			pick.return_value = "LOCAL-SN-1"
			to_reserve = _process_lines(order, lines)
		return order, to_reserve, pick

	def test_each_line_records_what_it_was_sold_under(self):
		"""Routing and invoicing read these, not the product's source at the time
		they run — a later switch must not move an order already taken."""
		order, _, _ = self._run(
			[_line("RSR-HOLOSUN", line_id="1"), _line("GLOCK-19", line_id="2")],
			firearm_items={"GLOCK-19"},
			dropship_items={"RSR-HOLOSUN": "RSR"}, modes={"RSR-HOLOSUN": "To Store"})
		got = [(l.lane, l.source_distributor, l.source_sku) for l in order.lines]
		self.assertEqual(got, [("to_store", "RSR", "SKU-RSR-HOLOSUN"), ("local", "", "")])

	def test_fds_firearm_row_is_empty_and_reserves_nothing(self):
		order, to_reserve, pick = self._run(
			[_line("RSR-GLOCK19")],
			firearm_items={"RSR-GLOCK19"},
			dropship_items={"RSR-GLOCK19": "RSR"},
			modes={"RSR-GLOCK19": "Direct"})

		self.assertEqual(len(order.serials), 1)
		row = order.serials[0]
		self.assertEqual(row.fulfillment, "fds")
		self.assertEqual(row.confirmed_serial, "")
		self.assertEqual(row.stock_hold_serial, "")
		self.assertEqual(row.reservation_error, "")
		self.assertEqual(to_reserve, [])
		pick.assert_not_called()

	def test_to_store_firearm_row_also_reserves_nothing(self):
		"""Procure-to-store: the gun does not exist in our stock yet either."""
		order, to_reserve, pick = self._run(
			[_line("RSR-GLOCK19")],
			firearm_items={"RSR-GLOCK19"},
			dropship_items={"RSR-GLOCK19": "RSR"},
			modes={"RSR-GLOCK19": "To Store"})
		self.assertEqual(order.serials[0].fulfillment, "to_store")
		self.assertEqual(to_reserve, [])
		pick.assert_not_called()

	def test_one_empty_row_per_unit(self):
		order, to_reserve, _ = self._run(
			[_line("RSR-GLOCK19", qty=3)],
			firearm_items={"RSR-GLOCK19"},
			dropship_items={"RSR-GLOCK19": "RSR"},
			modes={"RSR-GLOCK19": "Direct"})
		self.assertEqual(len(order.serials), 3)
		self.assertEqual([r.unit_index for r in order.serials], [0, 1, 2])
		self.assertTrue(all(r.fulfillment == "fds" for r in order.serials))
		self.assertEqual(to_reserve, [])

	def test_a_supplied_serial_on_a_dropship_line_is_still_not_reserved(self):
		"""A dealer-typed serial on a drop-ship line names a gun that is not ours;
		honouring it would reserve (and later ship) the wrong firearm."""
		line = _line("RSR-GLOCK19")
		line["meta_data"] = [{"key": "firearm_serials", "value": ["ABC123"]}]
		order, to_reserve, pick = self._run(
			[line],
			firearm_items={"RSR-GLOCK19"},
			dropship_items={"RSR-GLOCK19": "RSR"},
			modes={"RSR-GLOCK19": "Direct"})
		self.assertEqual(order.serials[0].confirmed_serial, "")
		self.assertEqual(to_reserve, [])
		pick.assert_not_called()

	def test_local_firearm_is_untouched(self):
		"""REGRESSION: the ordinary path must still hold a placeholder."""
		order, to_reserve, pick = self._run(
			[_line("GLOCK-19")], firearm_items={"GLOCK-19"})
		self.assertEqual(order.serials[0].fulfillment, "local")
		self.assertEqual(order.serials[0].stock_hold_serial, "LOCAL-SN-1")
		self.assertEqual(to_reserve, ["LOCAL-SN-1"])
		pick.assert_called_once()

	def test_dropship_accessory_creates_no_serial_row(self):
		order, to_reserve, _ = self._run(
			[_line("RSR-HOLOSUN")],
			dropship_items={"RSR-HOLOSUN": "RSR"},
			modes={"RSR-HOLOSUN": "Direct"})
		self.assertEqual(order.serials, [])
		self.assertEqual(to_reserve, [])


class TestDropshipStatusStamp(unittest.TestCase):
	"""The order is marked for routing INSIDE the insert, not by the async
	router — otherwise a `completed` webhook arriving first could auto-clear it
	before routing ever ran."""

	def _run(self, lines, *, firearm_items=(), dropship_items=None, modes=None):
		order = _Order()
		_gv = _gv_factory(firearm_items=firearm_items)

		with patch("frappe.db.get_value", side_effect=_gv), \
			 _source(dropship_items, modes), \
			 patch("ffl_woo_sync.woocommerce.webhook._pick_oldest_unreserved_serial",
				return_value="LOCAL-SN-1"):
			_process_lines(order, lines)
		return order

	def test_dropship_accessory_stamps_pending_route(self):
		order = self._run([_line("RSR-HOLOSUN")],
			dropship_items={"RSR-HOLOSUN": "RSR"}, modes={"RSR-HOLOSUN": "Direct"})
		self.assertEqual(order.dropship_status, "Pending Route")

	def test_mixed_order_stamps_pending_route(self):
		order = self._run(
			[_line("GLOCK-19", line_id="1"), _line("RSR-HOLOSUN", line_id="2")],
			firearm_items={"GLOCK-19"},
			dropship_items={"RSR-HOLOSUN": "RSR"}, modes={"RSR-HOLOSUN": "Direct"})
		self.assertEqual(order.dropship_status, "Pending Route")

	def test_wholly_local_order_is_not_stamped(self):
		order = self._run([_line("GLOCK-19")], firearm_items={"GLOCK-19"})
		self.assertFalse(order.dropship_status)


# ===================================================== auto-clear stands down

class TestAutoClearDropshipGuard(unittest.TestCase):

	def _order(self, **kw):
		base = {"order_status": "Pending Fulfillment", "woo_status": "completed",
			"serials": [], "dropship_status": None}
		base.update(kw)
		return frappe._dict(base)

	def test_unsettled_dropship_order_is_not_cleared(self):
		for status in ("Pending Route", "Ordered", "Error"):
			o = self._order(dropship_status=status)
			self.assertFalse(_auto_clear_non_firearm_if_completed(o), status)
			self.assertEqual(o.order_status, "Pending Fulfillment", status)

	def test_completed_dropship_order_is_cleared(self):
		o = self._order(dropship_status="Complete")
		self.assertTrue(_auto_clear_non_firearm_if_completed(o))
		self.assertEqual(o.order_status, "Transferred")

	def test_plain_local_order_still_clears(self):
		"""REGRESSION: no dropship_status at all is the overwhelming majority."""
		o = self._order()
		self.assertTrue(_auto_clear_non_firearm_if_completed(o))
		self.assertEqual(o.order_status, "Transferred")


# ============================================ cancel fires the hook

class TestCancelFiresHook(unittest.TestCase):

	def _cancel(self, hook_targets):
		body = {"id": 42, "status": "cancelled"}

		def _gv(doctype=None, name=None, field=None, *a, **k):
			if doctype == "Woo Online Order":
				return {"name": "WOO-ORD-00042", "site": "retail"}
			return None

		def _hooks(hook=None, *a, **k):
			return list(hook_targets) if hook == "woo_order_cancelled" else []

		calls = []
		with patch("frappe.db.get_value", side_effect=_gv), \
			 patch("frappe.get_all", return_value=[]), \
			 patch("frappe.db.set_value") as sv, \
			 patch("frappe.get_hooks", side_effect=_hooks), \
			 patch("frappe.get_attr", side_effect=lambda p: (
				lambda *a, **k: calls.append((p, a, k)))):
			_handle_order_cancelled(body)
		return calls, sv

	def test_hook_is_called_with_the_order_name(self):
		calls, _ = self._cancel(["osa_dropship.switch.woo_order_cancelled"])
		self.assertEqual(len(calls), 1)
		path, args, _ = calls[0]
		self.assertEqual(path, "osa_dropship.switch.woo_order_cancelled")
		self.assertEqual(args, ("WOO-ORD-00042",))

	def test_hook_runs_after_the_order_is_marked_cancelled(self):
		"""The consumer reads the order back; seeing it still open would let it
		decide the cancellation had not happened."""
		order = {"marked": False}

		def _gv(doctype=None, name=None, field=None, *a, **k):
			if doctype == "Woo Online Order":
				return {"name": "WOO-ORD-00042", "site": "retail"}
			return None

		seen = []

		def _sv(doctype, name, values, *a, **k):
			if doctype == "Woo Online Order":
				order["marked"] = True

		with patch("frappe.db.get_value", side_effect=_gv), \
			 patch("frappe.get_all", return_value=[]), \
			 patch("frappe.db.set_value", side_effect=_sv), \
			 patch("frappe.get_hooks", side_effect=lambda hook=None, *a, **k: (
				["x.y"] if hook == "woo_order_cancelled" else [])), \
			 patch("frappe.get_attr", side_effect=lambda p: (
				lambda *a, **k: seen.append(order["marked"]))):
			_handle_order_cancelled({"id": 42, "status": "cancelled"})

		self.assertEqual(seen, [True])

	def test_a_raising_consumer_does_not_break_the_cancel(self):
		"""Releasing the soft-reserve is the safety-critical half; a distributor
		bookkeeping failure must not strand a reserved gun."""
		def _gv(doctype=None, name=None, field=None, *a, **k):
			if doctype == "Woo Online Order":
				return {"name": "WOO-ORD-00042", "site": "retail"}
			return None

		def _boom(*a, **k):
			raise RuntimeError("router exploded")

		with patch("frappe.db.get_value", side_effect=_gv), \
			 patch("frappe.get_all", return_value=[]), \
			 patch("frappe.db.set_value"), \
			 patch("frappe.get_hooks", side_effect=lambda hook=None, *a, **k: (
				["x.y"] if hook == "woo_order_cancelled" else [])), \
			 patch("frappe.get_attr", return_value=_boom), \
			 patch("ffl_woo_sync.woocommerce.webhook._log_error") as log:
			_handle_order_cancelled({"id": 42, "status": "cancelled"})
		self.assertTrue(log.called)

	def test_no_registrant_is_a_noop(self):
		calls, sv = self._cancel([])
		self.assertEqual(calls, [])
		self.assertTrue(sv.called)


# ==================================== cross-app agreement on the lane rules

class TestLaneRulesAgreeAcrossApps(unittest.TestCase):
	"""``ffl_woo_sync`` may not import ``ffl_integrations`` (a frozen
	inversion), so the lane rule necessarily exists on both sides. This test is
	the anti-drift device: the webhook's marker and the router's classifier must
	answer identically for every combination.
	"""

	def test_matrix_agrees_with_router(self):
		from ffl_integrations.distributor import router

		for distributor in (None, "RSR"):
			for is_firearm in (False, True):
				for mode in ("Direct", "To Store", "", None):
					expected = router.classify_lane(
						dropship_distributor=distributor, is_firearm=is_firearm,
						fulfillment_mode=mode)

					with _source({"ITEM": distributor}, {"ITEM": mode}):
						got = _fulfillment_lane("ITEM", is_firearm=is_firearm)
					self.assertEqual(got, expected,
						f"{distributor=} {is_firearm=} {mode=}")

	def test_checkout_does_not_ask_for_the_lead_time(self):
		"""The line records its lane, house and SKU; a lead time is the push's."""
		with patch("ffl_woo_sync.woocommerce.mapping.dropship_source",
				return_value=None) as src:
			_fulfillment_lane("ITEM", is_firearm=False)
		src.assert_called_once_with("ITEM", lead=False)


if __name__ == "__main__":
	unittest.main()
