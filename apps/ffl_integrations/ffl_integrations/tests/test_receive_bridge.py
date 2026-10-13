"""P2 — receive_bridge: DO → Receive Goods pre-fill (Flow A restock into A&D).

Per [DIVERGENCE-3] the bridge is STAFF-GATED pre-fill, never cron auto-submit —
`create_receive` carries `frappe.only_for(("Stock Manager","System Manager"))` and
A&D acquisition is a compliance action a human must review. The payload builder,
the RSR-serial guard (len == qty) and the batch-arrival increment are PURE
(harness-run); the actual `create_receive` + received_qty bump + FFL Acquisition
provenance link run under `make test`.
"""
from __future__ import annotations

import unittest

import frappe

from ffl_integrations.distributor import receive_bridge as rb

# NOTE: the receive_from_do round-trip + staff gate (make test only) live in
# test_distributor_integration.py (FrappeTestCase). This file is pure logic.


def _line(sku, item_code=None, wish_qty=1, ack_qty=0, invoiced_qty=0, received_qty=0,
		substituted_qty=0, serials="", is_firearm=False, line_name=None, line_status=None, **kw):
	base = {
		"sku": sku, "item_code": item_code or sku, "wish_qty": wish_qty,
		"ack_qty": ack_qty, "invoiced_qty": invoiced_qty, "received_qty": received_qty,
		"substituted_qty": substituted_qty, "serials": serials, "is_firearm": is_firearm,
		"service_need": 0, "unit_cost": 10.0, "description": "desc", "line_name": line_name or sku,
	}
	if line_status is not None:
		base["line_status"] = line_status
	base.update(kw)
	return base


class TestReceivableIncrement(unittest.TestCase):
	def test_only_shipped_not_yet_received_qty_is_receivable(self):
		# invoiced 5, already received 2 -> this arrival is 3.
		lines = [_line("ACC", invoiced_qty=5, received_qty=2)]
		rec = rb._receivable_lines(lines)
		self.assertEqual(len(rec), 1)
		self.assertEqual(rec[0]["receive_qty"], 3)

	def test_nothing_shipped_yet_is_not_receivable(self):
		lines = [_line("ACC", ack_qty=2, invoiced_qty=0)]
		self.assertEqual(rb._receivable_lines(lines), [])

	def test_fully_received_line_is_skipped(self):
		lines = [_line("ACC", invoiced_qty=3, received_qty=3)]
		self.assertEqual(rb._receivable_lines(lines), [])

	def test_firearm_serials_apportioned_to_this_batch(self):
		# batch 2: cumulative invoiced 2, already received 1, cumulative serials [SN1,SN2]
		# -> this batch receives 1 unit with serial SN2 only.
		lines = [_line("GUN", invoiced_qty=2, received_qty=1, serials="SN1,SN2", is_firearm=True)]
		rec = rb._receivable_lines(lines)
		self.assertEqual(rec[0]["receive_qty"], 1)
		self.assertEqual(rec[0]["receive_serials"], ["SN2"])


class TestSerialGuard(unittest.TestCase):
	def test_firearm_receive_requires_serial_count_equal_qty(self):
		lines = [_line("GUN", invoiced_qty=2, received_qty=0, serials="SN1", is_firearm=True)]
		rec = rb._receivable_lines(lines)
		with self.assertRaises(frappe.ValidationError):
			rb._assert_serial_counts(rec)

	def test_matching_serial_count_passes(self):
		lines = [_line("GUN", invoiced_qty=1, received_qty=0, serials="SN1", is_firearm=True)]
		rec = rb._receivable_lines(lines)
		rb._assert_serial_counts(rec)  # no raise

	def test_accessory_needs_no_serials(self):
		lines = [_line("ACC", invoiced_qty=3, received_qty=0)]
		rec = rb._receivable_lines(lines)
		rb._assert_serial_counts(rec)  # no raise
		self.assertEqual(rec[0]["receive_serials"], [])


class TestShapePayload(unittest.TestCase):
	def _dist(self):
		return {"code": "RSR", "supplier": "RSR GROUP INC"}

	def test_payload_matches_create_receive_schema(self):
		rec = rb._receivable_lines([_line("ACC", item_code="ITEM-ACC", invoiced_qty=2)])
		payload = rb._shape_receive_payload("DO-2607-0001", self._dist(), rec, warehouse=None)
		self.assertEqual(payload["supplier"], "RSR GROUP INC")
		self.assertEqual(payload["source"], "RSR")
		self.assertEqual(payload["acquisition_type"], "Purchase")
		self.assertNotIn("warehouse", payload)  # None -> omitted, create_receive defaults
		item = payload["items"][0]
		self.assertEqual(item["item_code"], "ITEM-ACC")
		self.assertEqual(item["qty"], 2)
		self.assertEqual(item["rate"], 10.0)
		self.assertEqual(item["serials"], [])
		self.assertEqual(item["service_need"], 0)

	def test_firearm_line_carries_fastbound_detail_and_serials(self):
		rec = rb._receivable_lines([_line(
			"GUN", item_code="ITEM-GUN", invoiced_qty=1, serials="SN1", is_firearm=True,
			manufacturer="Glock", model_name="19", firearm_type="Pistol", caliber="9mm")])
		payload = rb._shape_receive_payload("DO-2607-0002", self._dist(), rec, warehouse=None)
		item = payload["items"][0]
		self.assertEqual(item["serials"], ["SN1"])
		self.assertEqual(item["manufacturer"], "Glock")
		self.assertEqual(item["model_name"], "19")
		self.assertEqual(item["firearm_type"], "Pistol")
		self.assertEqual(item["caliber"], "9mm")

	def test_payload_carries_distributor_order_provenance(self):
		rec = rb._receivable_lines([_line("ACC", invoiced_qty=1)])
		payload = rb._shape_receive_payload("DO-2607-0003", self._dist(), rec, warehouse=None)
		self.assertEqual(payload["distributor_order"], "DO-2607-0003")

	def test_warehouse_included_when_given(self):
		rec = rb._receivable_lines([_line("ACC", invoiced_qty=1)])
		payload = rb._shape_receive_payload("DO-1", self._dist(), rec, warehouse="Main - GS")
		self.assertEqual(payload["warehouse"], "Main - GS")

	def test_rate_is_omitted_not_zeroed_when_the_order_line_has_no_cost(self):
		"""`float(unit_cost or 0)` invented a number: it told create_receive "this
		was free" when what we knew was nothing. Omitting the key sends the truth,
		and the receive guard says so instead of booking a $0 gun."""
		rec = rb._receivable_lines([_line("ACC", invoiced_qty=1, unit_cost=0)])
		payload = rb._shape_receive_payload("DO-1", self._dist(), rec, warehouse=None)
		self.assertNotIn("rate", payload["items"][0])

	def test_a_real_cost_is_still_sent(self):
		rec = rb._receivable_lines([_line("ACC", invoiced_qty=1, unit_cost=9.5)])
		payload = rb._shape_receive_payload("DO-1", self._dist(), rec, warehouse=None)
		self.assertEqual(payload["items"][0]["rate"], 9.5)


class TestUnitCostGuard(unittest.TestCase):
	"""A distributor receive with no unit cost must fail on the ORDER, where a
	human can fix it, not at the door.

	receive_from_do is server-to-server — the DO's Receive dialog is a read-only
	preview with no cost box and no "it was free" checkbox — so the zero-cost
	guard firing inside create_receive would leave a firearm that is physically
	in the shop but in neither stock nor the A&D book, with nothing on screen to
	act on. Same placement and same reason as the serial-count guard above.
	"""

	def test_a_line_with_no_unit_cost_is_refused(self):
		rec = rb._receivable_lines([_line("ACC", invoiced_qty=2, unit_cost=0)])
		with self.assertRaises(frappe.ValidationError) as ctx:
			rb._assert_unit_costs(rec)
		# Actionable: names the SKU and the field to fill in.
		self.assertIn("ACC", str(ctx.exception))
		self.assertIn("Unit Cost", str(ctx.exception))

	def test_a_missing_unit_cost_key_is_refused(self):
		rec = rb._receivable_lines([_line("GUN", invoiced_qty=1, serials="SN1",
			is_firearm=True, unit_cost=None)])
		with self.assertRaises(frappe.ValidationError):
			rb._assert_unit_costs(rec)

	def test_priced_lines_pass(self):
		rec = rb._receivable_lines([_line("ACC", invoiced_qty=2, unit_cost=1.0)])
		rb._assert_unit_costs(rec)  # no raise

	def test_every_missing_sku_is_named_at_once(self):
		"""One trip to the order, not one refusal per line."""
		rec = rb._receivable_lines([
			_line("ACC-A", invoiced_qty=1, unit_cost=0, line_name="r1"),
			_line("ACC-B", invoiced_qty=1, unit_cost=5, line_name="r2"),
			_line("ACC-C", invoiced_qty=1, unit_cost=0, line_name="r3"),
		])
		with self.assertRaises(frappe.ValidationError) as ctx:
			rb._assert_unit_costs(rec)
		msg = str(ctx.exception)
		self.assertIn("ACC-A", msg)
		self.assertIn("ACC-C", msg)
		self.assertNotIn("ACC-B", msg)


class TestReceiveDeltas(unittest.TestCase):
	"""m8: received_qty must bump per LINE, not by item_code aggregate — two DO
	lines sharing an item_code must each get only their own increment."""

	def test_two_lines_same_item_code_bump_independently(self):
		lines = [
			_line("A", item_code="ITEM-X", invoiced_qty=1, line_name="row1"),
			_line("B", item_code="ITEM-X", invoiced_qty=2, line_name="row2"),
		]
		rec = rb._receivable_lines(lines)
		self.assertEqual(rb._receive_deltas(rec), [("row1", 1), ("row2", 2)])

	def test_delta_is_the_increment_not_cumulative(self):
		lines = [_line("A", item_code="ITEM-X", invoiced_qty=5, received_qty=2, line_name="row1")]
		rec = rb._receivable_lines(lines)
		self.assertEqual(rb._receive_deltas(rec), [("row1", 3)])


class TestOutstandingQty(unittest.TestCase):
	def test_acked_but_not_invoiced_counts(self):
		"""Parcels beat the poll: an acked line is already something to receive."""
		self.assertEqual(rb.outstanding_qty(_line("A", ack_qty=3)), 3)

	def test_invoiced_wins_when_larger(self):
		self.assertEqual(rb.outstanding_qty(_line("A", ack_qty=2, invoiced_qty=3)), 3)

	def test_received_and_substituted_come_off(self):
		line = _line("A", ack_qty=5, invoiced_qty=5, received_qty=2, substituted_qty=2)
		self.assertEqual(rb.outstanding_qty(line), 1)

	def test_never_negative(self):
		self.assertEqual(rb.outstanding_qty(_line("A", ack_qty=1, received_qty=3)), 0)

	def test_killed_line_is_nothing_to_wait_for(self):
		self.assertEqual(rb.outstanding_qty(_line("A", ack_qty=2, line_status="Killed")), 0)


class TestRankCandidates(unittest.TestCase):
	def _row(self, do, placed, idx=1, **kw):
		return {"do_name": do, "line_name": f"{do}-{idx}", "placed_at": placed, "idx": idx,
			"ack_qty": 1, "invoiced_qty": 0, "received_qty": 0, "substituted_qty": 0,
			"line_status": "Acked", **kw}

	def test_oldest_order_first(self):
		rows = [self._row("DO-2", "2026-09-15 10:00:00"), self._row("DO-1", "2026-09-14 10:00:00")]
		self.assertEqual([r["do_name"] for r in rb.rank_candidates(rows)], ["DO-1", "DO-2"])

	def test_nothing_outstanding_is_dropped(self):
		rows = [self._row("DO-1", "2026-09-14", received_qty=1),
			self._row("DO-2", "2026-09-15", substituted_qty=1),
			self._row("DO-3", "2026-09-16", line_status="Killed")]
		self.assertEqual(rb.rank_candidates(rows), [])

	def test_outstanding_is_annotated(self):
		out = rb.rank_candidates([self._row("DO-1", "2026-09-14", ack_qty=3, received_qty=1)])
		self.assertEqual(out[0]["outstanding"], 2)

	def test_unplaced_sorts_last(self):
		rows = [self._row("DO-N", None), self._row("DO-1", "2026-09-14")]
		self.assertEqual([r["do_name"] for r in rb.rank_candidates(rows)], ["DO-1", "DO-N"])


class TestFastBoundPoNumber(unittest.TestCase):
	"""M2b: the FastBound acquisition payload must carry the DO name as the
	purchase-order number when the acquisition was received against a DO."""

	def _acq(self, **kw):
		class FakeAcq(dict):
			__getattr__ = dict.get
		base = dict(name="ACQ-1", acquisition_date="2026-07-22", serial="SN1", item="ITEM-1",
			cost=100, source_type="FFL Dealer", source_ffl="1-23-456", source_name="RSR", notes="",
			company=frappe.db.get_single_value("Global Defaults", "default_company"))
		base.update(kw)
		return FakeAcq(base)

	def test_distributor_order_maps_to_purchase_order_number(self):
		from ffl_integrations.fastbound.client import FastBoundClient
		body = FastBoundClient._acquisition_payload(self._acq(distributor_order="DO-2607-0001"))
		self.assertEqual(_po_number(body), "DO-2607-0001")

	def test_no_distributor_order_no_po_number(self):
		from ffl_integrations.fastbound.client import FastBoundClient
		body = FastBoundClient._acquisition_payload(self._acq())
		self.assertIsNone(_po_number(body))


def _po_number(body):
	"""Read acquire_PurchaseOrderNumber wherever the payload carries it (acquisition
	body or per-item), so the test is robust to the exact placement."""
	if body.get("acquire_PurchaseOrderNumber"):
		return body["acquire_PurchaseOrderNumber"]
	for item in body.get("items", []):
		if item.get("acquire_PurchaseOrderNumber"):
			return item["acquire_PurchaseOrderNumber"]
	return None
