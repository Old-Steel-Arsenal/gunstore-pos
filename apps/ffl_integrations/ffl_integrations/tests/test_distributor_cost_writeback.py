"""Cost write-back: the poll carries the distributor's
invoiced unit cost and freight onto the Distributor Order; the receive books the
invoiced figure; a freight gap beyond the house's limit alerts once."""
from __future__ import annotations

import unittest
from unittest import mock

import frappe

from ffl_integrations.distributor import receive_bridge as rb
from ffl_integrations.distributor import rsr_provider, ss_provider, tasks


class Normalisers(unittest.TestCase):
	def test_ss_line_carries_the_invoiced_unit_cost_and_freight(self):
		out = ss_provider.normalize_order_status("1",
			[{"sku": "A", "filled_qty": 2, "ordered_qty": 2, "filled_price": "13.03"}],
			[{"S1INV": "INV1"}], [{"TRACKNO": "1Z", "SHPAMT": "10.95"}], [])
		self.assertEqual(out["lines"][0]["unit_cost"], 13.03)
		self.assertEqual(out["freight"], 10.95)

	def test_ss_line_without_a_price_is_unknown(self):
		out = ss_provider.normalize_order_status("1",
			[{"sku": "A", "filled_qty": 1, "ordered_qty": 1}], [], [], [])
		self.assertIsNone(out["lines"][0]["unit_cost"])

	def test_rsr_line_cost_and_freight_when_present(self):
		out = rsr_provider.normalize_order_status({"WebRef": "1", "FreightCharge": "$12.00",
			"Items": [{"PartNum": "A", "Invoiced": 1, "Cost": "$22.99"}]})
		self.assertEqual(out["lines"][0]["unit_cost"], 22.99)
		self.assertEqual(out["freight"], 12.0)

	def test_rsr_without_cost_leaves_it_unknown(self):
		out = rsr_provider.normalize_order_status({"Items": [{"PartNum": "A", "Invoiced": 1}]})
		self.assertIsNone(out["lines"][0]["unit_cost"])
		self.assertIsNone(out["freight"])


class Fold(unittest.TestCase):
	def _dl(self, **kw):
		return {"sku": "A", "wish_qty": 1, "ack_qty": 1, "invoiced_qty": 0, "received_qty": 0,
			"item_status": "", "invoices": "", "tracking_numbers": "", "serials": "",
			"line_status": "Acked", "invoiced_unit_cost": 0, **kw}

	def test_invoiced_unit_cost_is_folded(self):
		res = tasks.compute_do_updates([self._dl()], {"lines": [{"sku": "A", "invoiced_qty": 1,
			"unit_cost": 9.9}]})
		self.assertEqual(res["lines"]["A"]["invoiced_unit_cost"], 9.9)

	def test_missing_cost_keeps_the_old_value(self):
		res = tasks.compute_do_updates([self._dl(invoiced_unit_cost=9.9)],
			{"lines": [{"sku": "A", "invoiced_qty": 1}]})
		self.assertEqual(res["lines"]["A"]["invoiced_unit_cost"], 9.9)


class FreightVariance(unittest.TestCase):
	def test_gap_beyond_limit_is_returned(self):
		self.assertEqual(tasks.freight_variance(10.95, 0.0, 5.0), 10.95)
		self.assertIsNone(tasks.freight_variance(10.95, 8.0, 5.0))
		self.assertIsNone(tasks.freight_variance(None, 8.0, 5.0))
		self.assertEqual(tasks.freight_variance(0.0, 8.0, 5.0), -8.0)


class ReceivePayloadPrefersInvoicedCost(unittest.TestCase):
	def test_invoiced_cost_wins_over_the_order_line(self):
		line = {"sku": "A", "item_code": "A", "invoiced_qty": 1, "received_qty": 0,
			"serials": "", "is_firearm": False, "service_need": 0, "unit_cost": 1.0,
			"invoiced_unit_cost": 1.25, "line_name": "L1"}
		batch = rb._receivable_lines([line])
		payload = rb._shape_receive_payload("DO-1", {"code": "RSR", "supplier": "S"}, batch, None)
		self.assertEqual(payload["items"][0]["rate"], 1.25)

	def test_order_line_cost_when_nothing_invoiced_yet(self):
		line = {"sku": "A", "item_code": "A", "invoiced_qty": 1, "received_qty": 0,
			"serials": "", "is_firearm": False, "service_need": 0, "unit_cost": 1.0,
			"invoiced_unit_cost": 0, "line_name": "L1"}
		batch = rb._receivable_lines([line])
		payload = rb._shape_receive_payload("DO-1", {"code": "RSR", "supplier": "S"}, batch, None)
		self.assertEqual(payload["items"][0]["rate"], 1.0)


from ffl_integrations.tests.test_receive_incoming import _IncomingBase  # noqa: E402


class PollWritesBack(_IncomingBase):
	def _status(self, **kw):
		base = {"po_num": "", "web_ref": "1", "hold_status": "", "lines": [
			{"sku": "DC-ACC", "invoiced_qty": 1, "item_status": "", "invoices": ["INV1"],
				"tracking_numbers": ["1Z"], "serials": [], "unit_cost": 1.4}]}
		base.update(kw)
		return base

	def test_poll_writes_invoiced_cost_and_freight_and_alerts_on_a_gap(self):
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1, "ack_qty": 1,
			"unit_cost": 1.0}], status="Placed")
		frappe.db.set_value("Distributor Order", do.name,
			{"placed_at": "2026-09-14 10:00:00", "freight_cost": 0}, update_modified=False)
		provider = mock.Mock(get_order_status=lambda name: self._status(freight=10.95))
		with mock.patch.object(tasks, "_provider_for", return_value=provider), \
				mock.patch.object(tasks, "notify_admins") as alert:
			tasks.poll_order(do.name)
		self.assertAlmostEqual(float(frappe.db.get_value("Distributor Order Line",
			do.lines[0].name, "invoiced_unit_cost")), 1.4, places=2)
		self.assertAlmostEqual(float(frappe.db.get_value("Distributor Order", do.name,
			"invoiced_freight")), 10.95, places=2)
		subjects = [c.args[0] for c in alert.call_args_list]
		self.assertTrue(any("Freight differs" in s for s in subjects), subjects)

	def test_no_alert_when_freight_matches_or_is_unknown(self):
		do = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1, "ack_qty": 1,
			"unit_cost": 1.0}], status="Placed")
		frappe.db.set_value("Distributor Order", do.name,
			{"placed_at": "2026-09-14 10:00:00", "freight_cost": 10.95}, update_modified=False)
		provider = mock.Mock(get_order_status=lambda name: self._status(freight=10.95))
		with mock.patch.object(tasks, "_provider_for", return_value=provider), \
				mock.patch.object(tasks, "notify_admins") as alert:
			tasks.poll_order(do.name)
		self.assertFalse(any("Freight differs" in c.args[0] for c in alert.call_args_list))
		do2 = self._do([{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1, "ack_qty": 1,
			"unit_cost": 1.0}], status="Placed")
		frappe.db.set_value("Distributor Order", do2.name,
			{"placed_at": "2026-09-14 10:00:00", "freight_cost": 0}, update_modified=False)
		provider = mock.Mock(get_order_status=lambda name: self._status())   # no freight key
		with mock.patch.object(tasks, "_provider_for", return_value=provider), \
				mock.patch.object(tasks, "notify_admins") as alert:
			tasks.poll_order(do2.name)
		self.assertFalse(any("Freight differs" in c.args[0] for c in alert.call_args_list))
