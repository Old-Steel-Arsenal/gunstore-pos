"""P4c — procure-to-store (C2) web-order side, PURE half.

`assign_procured_serials` (the frappe mutation + soft-reserve) and the
`list_pending_web_orders` arrived flag are bench-tested in
test_procure_to_store_integration.py. Here we pin the two pure decisions:

* `plan_procured_assignments` — which received serials fill which empty to_store
  rows, idempotently;
* `to_store_ready` — when an order's to_store legs have all arrived.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ffl_woo_sync.woocommerce import fulfillment as ff


def _srow(item_code, fulfillment="to_store", confirmed_serial="", woo_line_id="1",
		unit_index=0):
	# frappe._dict so the assign path's `row.confirmed_serial = …` (attribute set on a
	# real child doc) works, while `.get()` in plan_procured_assignments is unaffected.
	return frappe._dict(
		item_code=item_code,
		fulfillment=fulfillment,
		confirmed_serial=confirmed_serial,
		woo_line_id=woo_line_id,
		unit_index=unit_index,
	)


class TestPlanProcuredAssignments(unittest.TestCase):
	"""fulfillment.plan_procured_assignments — auto-hang match: [(row, serial)]."""

	def test_fills_an_empty_to_store_row_with_its_item_serial(self):
		rows = [_srow("GLK19")]
		plan = ff.plan_procured_assignments(rows, {"GLK19": ["SN1"]})
		self.assertEqual([(id(r), s) for r, s in plan], [(id(rows[0]), "SN1")])

	def test_never_touches_a_non_to_store_row(self):
		# An FDS gun's serial is RSR's record-only echo, a direct/local row is not
		# ours to fill from a procure-to-store receive. Only to_store rows are hung.
		for lane in ("fds", "direct", "local", ""):
			rows = [_srow("GLK19", fulfillment=lane)]
			self.assertEqual(
				ff.plan_procured_assignments(rows, {"GLK19": ["SN1"]}), [],
				f"lane={lane!r} must not be auto-hung")

	def test_skips_an_already_filled_row_idempotent(self):
		rows = [_srow("GLK19", confirmed_serial="SN1")]
		self.assertEqual(ff.plan_procured_assignments(rows, {"GLK19": ["SN2"]}), [])

	def test_a_serial_already_on_the_order_is_not_reassigned(self):
		# Re-receiving the same batch must not hang SN1 a second time onto the other
		# empty row — SN1 is already confirmed on row 0.
		rows = [_srow("GLK19", confirmed_serial="SN1", unit_index=0),
			_srow("GLK19", unit_index=1)]
		plan = ff.plan_procured_assignments(rows, {"GLK19": ["SN1", "SN2"]})
		self.assertEqual([(r["unit_index"], s) for r, s in plan], [(1, "SN2")])

	def test_one_serial_per_row_in_order(self):
		rows = [_srow("GLK19", unit_index=0), _srow("GLK19", unit_index=1)]
		plan = ff.plan_procured_assignments(rows, {"GLK19": ["SN1", "SN2"]})
		self.assertEqual([(r["unit_index"], s) for r, s in plan], [(0, "SN1"), (1, "SN2")])

	def test_surplus_serials_are_left_unused(self):
		rows = [_srow("GLK19")]
		plan = ff.plan_procured_assignments(rows, {"GLK19": ["SN1", "SN2", "SN3"]})
		self.assertEqual([s for _r, s in plan], ["SN1"])

	def test_a_short_batch_leaves_remaining_rows_empty(self):
		rows = [_srow("GLK19", unit_index=0), _srow("GLK19", unit_index=1)]
		plan = ff.plan_procured_assignments(rows, {"GLK19": ["SN1"]})
		self.assertEqual([(r["unit_index"], s) for r, s in plan], [(0, "SN1")])

	def test_a_row_matches_only_its_own_item_code(self):
		rows = [_srow("GLK19"), _srow("SIG320")]
		plan = ff.plan_procured_assignments(rows, {"GLK19": ["SN1"]})
		self.assertEqual([(r["item_code"], s) for r, s in plan], [("GLK19", "SN1")])


class TestToStoreReady(unittest.TestCase):
	"""fulfillment.to_store_ready — the 'received, awaiting shipment' derivation for the queue."""

	def test_all_received_is_ready(self):
		self.assertTrue(ff.to_store_ready(["Received"]))
		self.assertTrue(ff.to_store_ready(["Received", "Received"]))

	def test_any_unreceived_leg_is_not_ready(self):
		self.assertFalse(ff.to_store_ready(["Received", "Partial"]))
		self.assertFalse(ff.to_store_ready(["Not Received"]))

	def test_no_legs_is_not_ready(self):
		# No to_store DO linked (a pure FDS/direct order) never reads "arrived".
		self.assertFalse(ff.to_store_ready([]))


class TestAssignProcuredSerials(unittest.TestCase):
	"""fulfillment.assign_procured_serials — the frappe write half of the auto-hang.
	Pins that soft-reserve goes through the canonical webhook helper, not a
	self-rolled set_value, and that a Cancelled order is never hung."""

	def _order(self, *, status="Pending Fulfillment", rows=None):
		return frappe._dict(
			name="WOO-1", order_status=status,
			serials=rows if rows is not None else [_srow("GLK19")],
			flags=frappe._dict(), save=MagicMock())

	def test_assigns_and_reserves_through_the_canonical_helper(self):
		# The soft-reserve MUST route through webhook._reserve_serial (the canonical
		# reservation helper), not a self-rolled Serial No set_value.
		order = self._order()
		with patch("frappe.get_doc", return_value=order), \
			patch("ffl_woo_sync.woocommerce.webhook._reserve_serial") as reserve:
			res = ff.assign_procured_serials("WOO-1", {"GLK19": ["SN1"]})
		self.assertEqual(order.serials[0].confirmed_serial, "SN1")
		order.save.assert_called_once()
		reserve.assert_called_once_with("SN1", "WOO-1")
		self.assertEqual(res["assigned"], ["SN1"])

	def test_a_cancelled_order_is_not_hung(self):
		# A gun must never be soft-reserved to a dead order. No row write, no
		# reserve — the goods sit unallocated for staff to handle.
		order = self._order(status="Cancelled")
		with patch("frappe.get_doc", return_value=order), \
			patch("ffl_woo_sync.woocommerce.webhook._reserve_serial") as reserve:
			res = ff.assign_procured_serials("WOO-1", {"GLK19": ["SN1"]})
		order.save.assert_not_called()
		reserve.assert_not_called()
		self.assertEqual(order.serials[0].confirmed_serial, "",
			"a cancelled order's serial rows are left untouched")
		self.assertEqual(res["assigned"], [])
