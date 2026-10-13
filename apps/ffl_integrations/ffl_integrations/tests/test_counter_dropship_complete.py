"""Counter drop-ship completion.

A counter Sales Invoice already booked revenue at the counter, so its completion is
lighter than the web path: no Transferred, no revenue SI. When every counter-sourced
Distributor Order is Complete, it books the RSR COST (Draft PI, reused
from the web path), records the shipment on the invoice as a comment, and emails the
customer a plain tracking note (v1). ``maybe_complete_source`` dispatches by source
doctype so a Woo order still runs ``complete_web_order``.

Harness-safe here: the dispatch, and the pure tracking-line builder. The real doc
comment / email / cost-PI insert are site-only (test_counter_dropship_integration).
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ffl_integrations.distributor import order_state, tasks

_dict = tasks.frappe._dict


class MaybeCompleteSourceDispatchTest(unittest.TestCase):
	def _dispatch(self, source_doctype, source_name, leg_statuses):
		calls = []

		def _legs(dt, name):
			return [_dict(name=f"DO-{i}", status=s, order_type="Dropship Accessory")
				for i, s in enumerate(leg_statuses)]

		ps = [
			mock.patch.object(tasks, "source_legs", _legs),
			mock.patch.object(tasks, "enqueue_distributor",
				lambda dotted, **kw: calls.append((dotted, kw))),
		]
		for p in ps:
			p.start()
			# Register each stop separately: unittest runs cleanups LIFO, which
			# unwinds overlapping patches correctly. Stopping them FIFO in one
			# cleanup restores a STUB as the 'original' when two patchers target
			# the same attribute — that leaked frappe.get_doc process-wide.
			self.addCleanup(p.stop)
		do = _dict(source_doctype=source_doctype, source_name=source_name)
		tasks.maybe_complete_source(do)
		return calls

	def test_counter_source_enqueues_complete_counter_order(self):
		calls = self._dispatch("Sales Invoice", "SINV-1", [order_state.COMPLETE])
		self.assertEqual(len(calls), 1)
		dotted, kw = calls[0]
		self.assertIn("complete_counter_order", dotted)
		self.assertEqual(kw.get("sales_invoice"), "SINV-1")

	def test_counter_source_incomplete_leg_is_a_noop(self):
		calls = self._dispatch(
			"Sales Invoice", "SINV-1", [order_state.COMPLETE, order_state.PLACED])
		self.assertEqual(calls, [])

	def test_unknown_source_is_a_noop(self):
		self.assertEqual(self._dispatch("Delivery Note", "DN-1",
			[order_state.COMPLETE]), [])


class CounterTrackingLinesTest(unittest.TestCase):
	"""The plain content shared by the SI comment and the customer email."""

	def _do(self, name, lines):
		return types.SimpleNamespace(name=name,
			lines=[_dict(**ln) for ln in lines])

	def test_one_line_per_parcel_with_tracking(self):
		docs = [self._do("DO-2601-0001", [
			{"item_code": "RSR-ACC", "sku": "A", "tracking_numbers": "1Z999",
				"serials": "", "fulfillment": "direct"}])]
		out = tasks.counter_tracking_lines(docs)
		self.assertEqual(len(out), 1)
		self.assertIn("1Z999", out[0])

	def test_serials_surface_for_a_firearm_leg(self):
		docs = [self._do("DO-2601-0002", [
			{"item_code": "RSR-GUN", "sku": "G", "tracking_numbers": "1Z777",
				"serials": "SN123", "fulfillment": "fds"}])]
		out = tasks.counter_tracking_lines(docs)
		self.assertTrue(any("SN123" in line for line in out))

	def test_no_tracking_yields_no_line(self):
		docs = [self._do("DO-3", [
			{"item_code": "X", "sku": "X", "tracking_numbers": "", "serials": ""}])]
		self.assertEqual(tasks.counter_tracking_lines(docs), [])


class CompleteCounterOrderToStoreLegTest(unittest.TestCase):
	"""A procure-to-store leg ships distributor → store, not to the buyer: its
	tracking must never reach the counter customer (comment or email) — the web
	path's C2 rule, applied to the counter completion."""

	def _run(self, legs):
		docs = {leg.name: leg for leg in legs}
		posted, emailed = [], []
		ps = [
			mock.patch.object(tasks, "source_legs", lambda dt, name: [
				_dict(name=d.name, status=d.status, order_type=d.order_type) for d in legs]),
			mock.patch.object(tasks.frappe, "get_doc", lambda dt, name: docs[name]),
			mock.patch.object(tasks, "book_cost_invoices", lambda d, si: []),
			mock.patch.object(tasks, "_post_counter_comment", lambda si, lines: posted.extend(lines)),
			mock.patch.object(tasks, "_email_counter_customer", lambda si, lines: emailed.extend(lines)),
			mock.patch.object(tasks, "_mark_parcels_reported", lambda do: None),
		]
		for p in ps:
			p.start()
			self.addCleanup(p.stop)
		return tasks.complete_counter_order("SINV-1"), posted, emailed

	def _leg(self, name, order_type, tracking):
		return _dict(name=name, status=order_state.COMPLETE, order_type=order_type,
			notes_posted="", lines=[_dict(item_code="X", sku="X",
				tracking_numbers=tracking, serials="")])

	def test_to_store_only_order_tells_the_customer_nothing(self):
		result, posted, emailed = self._run(
			[self._leg("DO-TS", "Procure To Store", "1ZSTORE")])
		self.assertEqual(result["status"], "complete")
		self.assertEqual((posted, emailed), ([], []))
		self.assertEqual(result["reported"], 0)

	def test_mixed_order_reports_only_the_drop_ship_parcel(self):
		result, posted, emailed = self._run([
			self._leg("DO-DS", "Dropship Accessory", "1ZBUYER"),
			self._leg("DO-TS", "Procure To Store", "1ZSTORE")])
		self.assertTrue(emailed)
		self.assertTrue(all("1ZSTORE" not in line and "DO-TS" not in line
			for line in emailed + posted))
		self.assertTrue(any("1ZBUYER" in line for line in emailed))


if __name__ == "__main__":
	unittest.main()
