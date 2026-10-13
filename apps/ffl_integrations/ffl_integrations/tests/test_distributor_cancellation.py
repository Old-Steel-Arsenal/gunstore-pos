"""The ONE source-cancellation routine both cancel paths run.

A Woo order cancelled by the shop and a counter Sales Invoice being cancelled/amended
ask their Distributor Orders the same three questions: what can still be withdrawn,
what is already LIVE at the distributor (no cancel API), and what has already SHIPPED.
Only the terminal action differs — the counter REFUSES the cancel, the web path lets
it through and alerts — so the classification, the withdrawal and the shipped alert
live here exactly once.

`LIVE_AT_DISTRIBUTOR` is deliberately NOT `POLLABLE_STATES`: polling is a scheduling
concept, and it excludes ERROR. An ERROR order may have reached RSR before it failed,
so a cancel must treat it as potentially live rather than silently withdraw it.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ffl_integrations.distributor import cancellation, order_state

_dict = cancellation.frappe._dict


class LiveStateSetTest(unittest.TestCase):
	def test_error_counts_as_live_at_the_distributor(self):
		self.assertIn(order_state.ERROR, order_state.LIVE_AT_DISTRIBUTOR)

	def test_all_post_placement_states_are_live(self):
		for st in (order_state.PLACED, order_state.ACKNOWLEDGED, order_state.HOLD,
				order_state.PARTIALLY_SHIPPED):
			self.assertIn(st, order_state.LIVE_AT_DISTRIBUTOR)

	def test_not_conflated_with_the_polling_set(self):
		# POLLABLE is a scheduling concept; reusing it here would silently withdraw
		# an ERROR order that may already be live.
		self.assertNotEqual(order_state.LIVE_AT_DISTRIBUTOR, order_state.POLLABLE_STATES)

	def test_pre_placement_and_terminal_are_not_live(self):
		for st in (order_state.DRAFT, order_state.QUEUED, order_state.COMPLETE,
				order_state.CANCELLED):
			self.assertNotIn(st, order_state.LIVE_AT_DISTRIBUTOR)


class ClassifyTest(unittest.TestCase):
	def _c(self, *pairs):
		return cancellation.classify([{"name": n, "status": s} for n, s in pairs])

	def test_draft_and_queued_are_withdrawable(self):
		out = self._c(("DO-1", order_state.DRAFT), ("DO-2", order_state.QUEUED))
		self.assertEqual(out["withdraw"], ["DO-1", "DO-2"])
		self.assertEqual(out["live"], [])
		self.assertEqual(out["shipped"], [])

	def test_error_lands_in_live_not_withdraw(self):
		out = self._c(("DO-1", order_state.ERROR))
		self.assertEqual(out["live"], ["DO-1"])
		self.assertEqual(out["withdraw"], [])

	def test_complete_is_shipped_not_live(self):
		out = self._c(("DO-1", order_state.COMPLETE))
		self.assertEqual(out["shipped"], ["DO-1"])
		self.assertEqual(out["live"], [])

	def test_already_cancelled_needs_nothing(self):
		out = self._c(("DO-1", order_state.CANCELLED))
		self.assertEqual((out["withdraw"], out["live"], out["shipped"]), ([], [], []))

	def test_mixed_order_splits_three_ways(self):
		out = self._c(("D", order_state.DRAFT), ("L", order_state.PLACED),
			("S", order_state.COMPLETE), ("X", order_state.CANCELLED))
		self.assertEqual((out["withdraw"], out["live"], out["shipped"]),
			(["D"], ["L"], ["S"]))


class WithdrawTocTouTest(unittest.TestCase):
	"""Re-read each order under `for_update` before writing — a placement that
	lands between the classify read and this write must NOT be overwritten."""

	def _run(self, locked_status):
		saved = []

		def _get_value(dt, name=None, field=None, *a, **k):
			return locked_status

		def _get_doc(dt, name=None, *a, **k):
			d = cancellation.frappe._dict(name=name, status=order_state.DRAFT)
			d.save = lambda *a, **k: saved.append((d.name, d.status))
			return d

		ps = [
			mock.patch.object(cancellation.frappe.db, "get_value", _get_value),
			mock.patch.object(cancellation.frappe, "get_doc", _get_doc),
		]
		for p in ps:
			p.start()
			# Register each stop separately: unittest runs cleanups LIFO, which
			# unwinds overlapping patches correctly. Stopping them FIFO in one
			# cleanup restores a STUB as the 'original' when two patchers target
			# the same attribute — that leaked frappe.get_doc process-wide.
			self.addCleanup(p.stop)
		return cancellation.withdraw_orders(["DO-1"], "because"), saved

	def test_still_draft_is_withdrawn(self):
		done, saved = self._run(order_state.DRAFT)
		self.assertEqual(done, ["DO-1"])
		self.assertEqual(saved, [("DO-1", order_state.CANCELLED)])

	def test_placed_between_read_and_write_is_left_alone(self):
		done, saved = self._run(order_state.PLACED)
		self.assertEqual(done, [])
		self.assertEqual(saved, [])   # never overwrite a live order


if __name__ == "__main__":
	unittest.main()
