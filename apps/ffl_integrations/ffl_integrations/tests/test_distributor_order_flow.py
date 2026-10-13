"""Distributor Order flow: the non-submittable state machine, the poll
apply that folds an RSR order-status response onto DO lines, per-order poll
backoff / stale / FDS-hold escalation, and the idempotent place path.

The state machine, poll-apply and backoff predicates are PURE (harness-run). The
DO document lifecycle (real get_doc/save, roles) is guarded to run only under
`make test` (skipped when P2_HARNESS is set).
"""
from __future__ import annotations

import types
import unittest

from ffl_integrations.distributor import order_state
from ffl_integrations.distributor import tasks
from ffl_integrations.distributor.rsr_provider import (
	RSRProvider,
	build_place_order_payload,
	normalize_order_status,
	normalize_place_response,
)

# NOTE: frappe-integration cases (DO doc lifecycle, poll_order, place_order_job,
# receive_from_do) live in test_distributor_integration.py (FrappeTestCase, make
# test only). This file is pure logic — harness-runnable, no DB.


# ----------------------------------------------------------------- state machine

class TestOrderStateMachine(unittest.TestCase):
	def test_legal_transitions(self):
		for a, b in [
			("Draft", "Queued"),
			("Draft", "Cancelled"),
			("Queued", "Placed"),
			("Queued", "Error"),
			("Placed", "Acknowledged"),
			("Placed", "Hold"),
			("Acknowledged", "Hold"),
			("Hold", "Acknowledged"),
			("Acknowledged", "Partially Shipped"),
			("Partially Shipped", "Complete"),
			("Placed", "Error"),
			("Error", "Queued"),
			("Placed", "Cancelled"),
		]:
			self.assertTrue(order_state.can_transition(a, b), f"{a}->{b} should be legal")

	def test_illegal_transitions(self):
		for a, b in [
			("Draft", "Placed"),        # cannot skip Queued
			("Complete", "Placed"),     # terminal, no going back
			("Complete", "Partially Shipped"),
			("Cancelled", "Queued"),    # terminal
			("Placed", "Draft"),        # no un-place
			("Queued", "Complete"),     # cannot skip placement
		]:
			self.assertFalse(order_state.can_transition(a, b), f"{a}->{b} should be illegal")

	def test_same_state_is_a_noop(self):
		self.assertTrue(order_state.can_transition("Partially Shipped", "Partially Shipped"))

	def test_assert_transition_raises_on_illegal(self):
		with self.assertRaises(ValueError):
			order_state.assert_transition("Complete", "Placed")

	def test_pollable_and_terminal_sets(self):
		self.assertEqual(
			order_state.POLLABLE_STATES,
			{"Placed", "Acknowledged", "Hold", "Partially Shipped"})
		self.assertEqual(order_state.TERMINAL_STATES, {"Complete", "Cancelled"})


# ----------------------------------------------------------------- poll apply

def _do_line(sku, wish_qty=1, **kw):
	base = {"sku": sku, "wish_qty": wish_qty, "ack_qty": 0, "invoiced_qty": 0,
		"received_qty": 0, "serials": "", "tracking_numbers": "", "invoices": "",
		"item_status": "", "line_status": "Pending"}
	base.update(kw)
	return base


class TestPollApply(unittest.TestCase):
	"""Fixtures mirror LIVE check-order rows (2026-08-18 test-env sweep): keyed
	by PartNum, quantities in Invoiced/InvoiceQty, CSV-string echo fields — and
	NO AckQty (that exists only in the place-order response, so the DO line
	carries it and the poll folds it forward)."""

	def test_ack_carried_forward_moves_order_to_acknowledged(self):
		# Placement wrote ack_qty=2; the poll (no AckQty in check-order) must
		# carry it, not wipe it to 0 and demote the line back to Pending.
		lines = [_do_line("A", wish_qty=2, ack_qty=2)]
		status = normalize_order_status({"PONum": "DO-1", "Items": [
			{"PartNum": "A", "Invoiced": 0, "InvoiceQty": "", "TrackingNum": ""}]})
		res = tasks.compute_do_updates(lines, status)
		self.assertEqual(res["status"], "Acknowledged")
		self.assertEqual(res["lines"]["A"]["ack_qty"], 2)
		self.assertEqual(res["lines"]["A"]["line_status"], "Acked")

	def test_serials_land_on_the_matching_line(self):
		lines = [_do_line("G", wish_qty=1, ack_qty=1)]
		status = normalize_order_status(
			{"Items": [{"PartNum": "G", "Invoiced": 1,
				"SerialNum": "SN123", "TrackingNum": "1Z999", "Invoices": "INV-7"}]})
		res = tasks.compute_do_updates(lines, status)
		self.assertEqual(res["lines"]["G"]["serials"], "SN123")
		self.assertEqual(res["lines"]["G"]["tracking_numbers"], "1Z999")
		self.assertEqual(res["lines"]["G"]["invoices"], "INV-7")

	def test_partial_shipment_across_lines(self):
		lines = [_do_line("A", wish_qty=1, ack_qty=1), _do_line("B", wish_qty=1, ack_qty=1)]
		status = normalize_order_status({"Items": [
			{"PartNum": "A", "Invoiced": 1},   # shipped
			{"PartNum": "B", "Invoiced": 0},   # acked only (ack rides the DO line)
		]})
		res = tasks.compute_do_updates(lines, status)
		self.assertEqual(res["status"], "Partially Shipped")

	def test_all_invoiced_completes(self):
		lines = [_do_line("A", wish_qty=1, ack_qty=1), _do_line("B", wish_qty=2, ack_qty=2)]
		status = normalize_order_status({"Items": [
			{"PartNum": "A", "Invoiced": 1},
			{"PartNum": "B", "Invoiced": 2},
		]})
		res = tasks.compute_do_updates(lines, status)
		self.assertEqual(res["status"], "Complete")
		self.assertEqual(res["lines"]["B"]["line_status"], "Shipped")

	def test_hold_status_forces_hold(self):
		lines = [_do_line("A", wish_qty=1, ack_qty=1)]
		status = normalize_order_status(
			{"HoldStatus": "FDS Hold", "Items": [{"PartNum": "A", "Invoiced": 1}]})
		res = tasks.compute_do_updates(lines, status)
		self.assertEqual(res["status"], "Hold")
		self.assertEqual(res["hold_status"], "FDS Hold")

	def test_apply_does_not_mutate_inputs(self):
		lines = [_do_line("A", wish_qty=1)]
		status = normalize_order_status({"Items": [{"PartNum": "A", "Invoiced": 1}]})
		tasks.compute_do_updates(lines, status)
		self.assertEqual(lines[0]["invoiced_qty"], 0)  # original untouched

	def test_status_with_explicit_ack_still_wins_over_the_line(self):
		# Carry-forward is for a MISSING key only: a provider that does report
		# ack_qty (place response replayed, or a future distributor) must win.
		lines = [_do_line("A", wish_qty=2, ack_qty=2)]
		res = tasks.compute_do_updates(
			lines, {"hold_status": "", "lines": [{"sku": "A", "ack_qty": 1,
				"invoiced_qty": 0, "item_status": ""}]})
		self.assertEqual(res["lines"]["A"]["ack_qty"], 1)

	def test_all_killed_order_reaches_terminal_complete(self):
		# m7: RSR kills every line → the order is settled (nothing more will come),
		# so it must terminalise (Complete), not linger in Placed until the watchdog.
		lines = [_do_line("A", wish_qty=1), _do_line("B", wish_qty=1)]
		status = normalize_order_status({"Items": [
			{"PartNum": "A", "ItemStatus": "Killed"}, {"PartNum": "B", "ItemStatus": "Killed"}]})
		res = tasks.compute_do_updates(lines, status)
		self.assertEqual(res["status"], "Complete")
		self.assertEqual(res["lines"]["A"]["line_status"], "Killed")

	def test_shipped_plus_killed_completes(self):
		# m7: shippable line shipped, the rest killed → all settled → Complete.
		lines = [_do_line("A", wish_qty=1, ack_qty=1), _do_line("B", wish_qty=1)]
		status = normalize_order_status({"Items": [
			{"PartNum": "A", "Invoiced": 1}, {"PartNum": "B", "ItemStatus": "Killed"}]})
		res = tasks.compute_do_updates(lines, status)
		self.assertEqual(res["status"], "Complete")

	def test_acked_plus_killed_is_not_yet_complete(self):
		# A line still merely Acked (not shipped/killed) means the order isn't settled.
		lines = [_do_line("A", wish_qty=1, ack_qty=1), _do_line("B", wish_qty=1)]
		status = normalize_order_status({"Items": [
			{"PartNum": "A", "Invoiced": 0}, {"PartNum": "B", "ItemStatus": "Killed"}]})
		res = tasks.compute_do_updates(lines, status)
		self.assertNotEqual(res["status"], "Complete")

	# Live check-order rows, 2026-09-17 prod. DO-YYMM-0004: invoiced, not yet out
	# the door. DO-YYMM-0001: the same state a poll earlier, then shipped.
	INVOICED_AWAITING_SHIPMENT = {
		"Cost": "$871.00", "NFA": "N", "UPCcode": "0022188904888", "Invoiced": 1,
		"InvoiceQty": "1,0,", "Invoices": "4022788888,", "DateShipped": "Pending,",
		"TrackingNum": "Pending,", "SerialNum": "SDY7734,SubTooHigh,",
		"PartNum": "SW14670", "ASNQty": "-1", "Warehouse": ""}
	SHIPPED = {
		"Cost": "$22.99", "NFA": "N", "UPCcode": "0873750002996", "Invoiced": 1,
		"InvoiceQty": "1,", "Invoices": "4022784238,", "DateShipped": "20260916,",
		"TrackingNum": "9400151903965205304949,", "SerialNum": "",
		"PartNum": "MPIMAG980", "ASNQty": "1", "Warehouse": "Texas"}

	def test_invoiced_awaiting_shipment_is_not_shipped(self):
		# RSR's "Pending"/"SubTooHigh" placeholders are not tracking or serials,
		# and an invoice without a parcel must keep the order pollable.
		lines = [_do_line("SW14670", wish_qty=1, ack_qty=1)]
		status = normalize_order_status({"Items": [self.INVOICED_AWAITING_SHIPMENT]})
		line = status["lines"][0]
		self.assertEqual(line["invoiced_qty"], 0)
		self.assertEqual(line["tracking_numbers"], [])
		self.assertEqual(line["date_shipped"], [])
		self.assertEqual(line["serials"], ["SDY7734"])
		self.assertEqual(line["invoices"], ["4022788888"])
		res = tasks.compute_do_updates(lines, status)
		self.assertEqual(res["lines"]["SW14670"]["line_status"], "Acked")
		self.assertEqual(res["lines"]["SW14670"]["tracking_numbers"], "")
		self.assertEqual(res["status"], "Acknowledged")
		self.assertIn(res["status"], order_state.POLLABLE_STATES)

	def test_shipment_after_pending_completes_with_real_tracking(self):
		lines = [_do_line("MPIMAG980", wish_qty=1, ack_qty=1)]
		pending = dict(self.SHIPPED, InvoiceQty="1,0,", DateShipped="Pending,",
			TrackingNum="Pending,", SerialNum="SubTooHigh,", ASNQty="-1", Warehouse="")
		first = tasks.compute_do_updates(lines, normalize_order_status({"Items": [pending]}))
		self.assertEqual(first["status"], "Acknowledged")
		polled = [dict(lines[0], **first["lines"]["MPIMAG980"])]
		res = tasks.compute_do_updates(polled, normalize_order_status({"Items": [self.SHIPPED]}))
		self.assertEqual(res["status"], "Complete")
		self.assertEqual(res["lines"]["MPIMAG980"]["line_status"], "Shipped")
		self.assertEqual(res["lines"]["MPIMAG980"]["tracking_numbers"], "9400151903965205304949")
		self.assertEqual(res["lines"]["MPIMAG980"]["serials"], "")

	def test_shipped_serial_survives_a_later_blank_serial_echo(self):
		# The pending poll echoed the gun's serial; a shipped row that echoes ""
		# must not wipe it (the receive page pre-fills from it).
		lines = [_do_line("SW14670", wish_qty=1, ack_qty=1, serials="SDY7734")]
		shipped = dict(self.INVOICED_AWAITING_SHIPMENT, DateShipped="20260918,",
			TrackingNum="1Z999AA10123456784,", SerialNum="")
		res = tasks.compute_do_updates(lines, normalize_order_status({"Items": [shipped]}))
		self.assertEqual(res["lines"]["SW14670"]["serials"], "SDY7734")
		self.assertEqual(res["status"], "Complete")


class TestPollSchedule(unittest.TestCase):
	"""Per-order backoff must progress 30m→1h→2h→2h across successive polls.
	Exercises the poll_order scheduling WIRING (compute-before-overwrite), which the
	pure `_next_interval` test cannot see."""

	def _do(self, placed_at):
		return types.SimpleNamespace(placed_at=placed_at, last_poll_at=None, next_poll_at=None)

	def test_backoff_progresses_and_does_not_reset_to_30m(self):
		from datetime import datetime
		T0 = datetime(2026, 7, 22, 10, 0, 0)
		do = self._do(T0)
		# placement schedules the first poll at +30m; last_poll_at stays None
		do.next_poll_at = tasks._compute_next_poll_at(do, T0)
		self.assertEqual((do.next_poll_at - T0).total_seconds(), 1800)
		self.assertIsNone(do.last_poll_at)
		intervals = []
		for _ in range(3):
			now = do.next_poll_at
			tasks.advance_schedule(do, now)
			intervals.append((do.next_poll_at - now).total_seconds())
			self.assertEqual(do.last_poll_at, now)
		self.assertEqual(intervals, [3600, 7200, 7200])  # 1h → 2h → 2h cap (NOT 1800,1800,1800)


# ----------------------------------------------------------------- backoff / stale

class TestPollBackoff(unittest.TestCase):
	def test_interval_doubles_from_30m_to_2h_cap(self):
		self.assertEqual(tasks._next_interval(0), 1800)          # 30m start
		self.assertEqual(tasks._next_interval(1800), 3600)       # 1h
		self.assertEqual(tasks._next_interval(3600), 7200)       # 2h
		self.assertEqual(tasks._next_interval(7200), 7200)       # cap
		self.assertEqual(tasks._next_interval(100000), 7200)     # never exceeds cap

	def test_stale_after_14_days(self):
		day = 86400
		self.assertFalse(tasks._is_stale(13 * day))
		self.assertTrue(tasks._is_stale(14 * day))
		self.assertTrue(tasks._is_stale(20 * day))

	def test_fds_hold_escalation_levels(self):
		day = 86400
		self.assertIsNone(tasks._hold_escalation_level(2 * day))
		self.assertEqual(tasks._hold_escalation_level(3 * day), "day3")
		self.assertEqual(tasks._hold_escalation_level(int(4.5 * day)), "day4.5")
		self.assertEqual(tasks._hold_escalation_level(6 * day), "day4.5")


# ----------------------------------------------------------------- place order

class _FakeLine(dict):
	__getattr__ = dict.get


class _FakeDO:
	def __init__(self, name, account="dealer", fill_or_kill=False, lines=None,
			order_type="Stocking", **fields):
		self.name = name
		self.account = account
		self.fill_or_kill = fill_or_kill
		self.lines = lines or []
		self.order_type = order_type
		for k, v in fields.items():
			setattr(self, k, v)


class TestPlaceOrderPayload(unittest.TestCase):
	"""Payload shapes are the LIVE contract (2026-08-18 sweep): PartNum/WishQty
	line keys (the old SKU/Quantity shape drew 93 "No valid items submitted"),
	required Email, and mode-dependent drop-ship fields."""

	def test_payload_uses_do_name_as_ponum(self):
		do = _FakeDO("DO-2607-0001", lines=[
			_FakeLine(sku="A", wish_qty=3, ship_method="Grnd"),
			_FakeLine(sku="B", wish_qty=1, ship_method="2Day"),
		])
		payload = build_place_order_payload(do, "orders@example.com")
		self.assertEqual(payload["PONum"], "DO-2607-0001")
		self.assertEqual(payload["Email"], "orders@example.com")
		self.assertEqual(payload["Items"], [
			{"PartNum": "A", "WishQty": 3, "ShipMethod": "Grnd"},
			{"PartNum": "B", "WishQty": 1, "ShipMethod": "2Day"},
		])

	def test_fill_or_kill_only_sent_when_on(self):
		# The manual documents only "Y"; omission IS the off state.
		off = build_place_order_payload(
			_FakeDO("DO-1", lines=[_FakeLine(sku="A", wish_qty=1)]), "e@example.com")
		self.assertNotIn("FillOrKill", off)
		on = build_place_order_payload(
			_FakeDO("DO-2", fill_or_kill=True, lines=[_FakeLine(sku="A", wish_qty=1)]),
			"e@example.com")
		self.assertEqual(on["FillOrKill"], "Y")

	def test_stocking_order_carries_no_dropship_fields(self):
		payload = build_place_order_payload(
			_FakeDO("DO-3", lines=[_FakeLine(sku="A", wish_qty=1)]), "e@example.com")
		for key in ("Storename", "ContactNum", "ShipFFL", "ShipAddress"):
			self.assertNotIn(key, payload)

	def test_fds_order_sends_ffl_and_consumer_but_never_an_address(self):
		# Live FieldWarnings: a supplied address on an FFL shipment is ignored —
		# RSR ships to the transfer dealer's ON-FILE premises.
		do = _FakeDO("DO-4", account="fulfillment", order_type="Dropship Firearm",
			lines=[_FakeLine(sku="GLK19", wish_qty=1)],
			consumer_name="John Buyer", contact_num="5125550142",
			transfer_ffl_license="9-99-999-07-7E-17760",
			ship_address_line1="100 N 1ST AVE", ship_address_city="PHOENIX",
			ship_address_state="AZ", ship_address_zip="85001")
		payload = build_place_order_payload(do, "e@example.com")
		self.assertEqual(payload["Storename"], "John Buyer")
		self.assertEqual(payload["ContactNum"], "5125550142")
		self.assertEqual(payload["ShipFFL"], "9-99-999-07-7E-17760")
		for key in ("ShipAddress", "ShipAddress2", "ShipCity", "ShipState", "ShipZip"):
			self.assertNotIn(key, payload)

	def test_accessory_dropship_sends_address_but_never_an_ffl(self):
		# Live StatusCode 84: "FFL provided with an accessories only order".
		do = _FakeDO("DO-5", account="fulfillment", order_type="Dropship Accessory",
			lines=[_FakeLine(sku="MAG1", wish_qty=2)],
			consumer_name="Jane Buyer", contact_num="5125550143",
			transfer_ffl_license="",
			ship_address_line1="200 Commerce Dr", ship_address_line2="",
			ship_address_city="Winter Park", ship_address_state="FL",
			ship_address_zip="32792")
		payload = build_place_order_payload(do, "e@example.com")
		self.assertEqual(payload["Storename"], "Jane Buyer")
		self.assertEqual(payload["ShipAddress"], "200 Commerce Dr")
		self.assertEqual(payload["ShipCity"], "Winter Park")
		self.assertEqual(payload["ShipState"], "FL")
		self.assertEqual(payload["ShipZip"], "32792")
		self.assertNotIn("ShipFFL", payload)
		self.assertNotIn("ShipAddress2", payload)   # empty line2 stays off the wire


class TestPlaceOrderIdempotent(unittest.TestCase):
	def _mock_client(self, calls, order_email="orders@example.com"):
		class MockClient:
			config = types.SimpleNamespace(order_email=order_email)

			def place_order_idempotent(self, po_num, payload, account):
				calls.append((po_num, payload, account))
				# live shape: PartNum-keyed lines, $-prefixed money
				return {"PONum": po_num, "WebRef": "W-99", "Items": [
					{"PartNum": "A", "UPCcode": "0873750008264", "AckQty": 1,
						"Cost": "$12.50", "ItemStatus": "Item was fulfilled."}]}

			def place_order(self, payload, account):  # must NOT be used directly
				raise AssertionError("place_order (blind) must never be called")
		return MockClient()

	def test_place_uses_idempotent_path_with_ponum_and_never_double_orders(self):
		calls = []
		do = _FakeDO("DO-2607-0003", account="dealer", lines=[_FakeLine(sku="A", wish_qty=1)])
		result = RSRProvider()._place(do, self._mock_client(calls))

		self.assertEqual(len(calls), 1, "exactly one placement call")
		self.assertEqual(calls[0][0], "DO-2607-0003", "PONum == DO name")
		self.assertEqual(calls[0][1]["Email"], "orders@example.com")
		self.assertEqual(calls[0][2], "dealer")
		self.assertEqual(result["web_ref"], "W-99")
		self.assertEqual(result["ack_lines"][0]["ack_qty"], 1)
		self.assertEqual(result["ack_lines"][0]["unit_cost"], 12.5)

	def test_place_fails_closed_without_an_order_email(self):
		# place-order's Email is required (StatusCode 88) — an empty setting must
		# stop BEFORE the wire, as a retryable config fault.
		from ffl_integrations.rsr.api_client import RSRConfigError

		calls = []
		do = _FakeDO("DO-2607-0004", account="dealer", lines=[_FakeLine(sku="A", wish_qty=1)])
		with self.assertRaises(RSRConfigError):
			RSRProvider()._place(do, self._mock_client(calls, order_email=""))
		self.assertEqual(calls, [], "nothing may reach the wire")


class TestNormalizeResponses(unittest.TestCase):
	def test_normalize_place_response_live_shape(self):
		"""Verbatim live place-order response (test env, WebRef 2764432):
		PartNum-keyed lines, $-prefixed money, ConfirmResp==WebRef."""
		r = normalize_place_response({
			"Status": "OK", "FieldWarnings": [], "DefaultPayment": "OPEN::",
			"Items": [{"UPCcode": "672352241006", "PartNum": "BD100",
				"Cost": "$13.75", "PartShippedHow": "FedEx Ground",
				"PartShippedRequested": "FedEx Ground", "AckQty": 1,
				"FullyFilled": "Y", "ItemStatus": "Item was fulfilled."}],
			"Shipping": "$10.00", "ShippingNCOD": "$10.00",
			"Subtotal": "$13.75", "Total": "$23.75",
			"StatusCode": "00", "StatusMssg": "",
			"ConfirmResp": "2764432", "WebRef": "2764432", "HoldStatus": ""})
		self.assertEqual(r["web_ref"], "2764432")
		self.assertEqual(r["hold_status"], "")
		self.assertFalse(r["queued"])
		line = r["ack_lines"][0]
		self.assertEqual(line["sku"], "BD100")
		self.assertEqual(line["ack_qty"], 1)
		self.assertEqual(line["unit_cost"], 13.75)
		self.assertEqual(line["fully_filled"], "Y")
		self.assertEqual(r["freight"], 10.0)
		self.assertEqual(r["totals"], {"subtotal": 13.75, "total": 23.75})

	def test_web_ref_falls_back_to_confirm_resp(self):
		r = normalize_place_response({"ConfirmResp": "1000001", "Items": []})
		self.assertEqual(r["web_ref"], "1000001")

	def test_hold_status_is_authoritative_over_the_fdshold_flag(self):
		"""Live contradiction: an order holding as "FDS Hold" answered FDSHold
		"N" — the flag must never override HoldStatus."""
		r = normalize_place_response({"WebRef": "1000001",
			"HoldStatus": "FDS Hold", "FDSHold": "N", "Items": []})
		self.assertEqual(r["hold_status"], "FDS Hold")

	def test_status_60_marks_the_order_queued(self):
		r = normalize_place_response({"StatusCode": "60", "WebRef": "W1", "Items": []})
		self.assertTrue(r["queued"])

	def test_place_response_surfaces_the_freight_charge(self):
		"""The distributor's shipping charge is the ONLY source of the PI freight
		line. It rides the place-order response; without capturing it here,
		the freight line is permanently unreachable. Live values are $-prefixed strings."""
		for key in ("Freight", "Shipping", "FreightCharge"):
			r = normalize_place_response({"PONum": "DO-1", key: "$24.99", "Items": []})
			self.assertEqual(r["freight"], 24.99, key)

	def test_place_response_freight_defaults_to_zero_when_absent(self):
		r = normalize_place_response({"PONum": "DO-1", "Items": []})
		self.assertEqual(r["freight"], 0.0)

	def test_normalize_order_status_live_shape(self):
		"""check-order row: FIELD NAMES are live-verified (unshipped row of
		TEST-20260818-03); the shipped-side VALUES (Invoiced 1, serial/
		tracking/invoice strings) are constructed — no test order ever shipped,
		so their types are the manual's word until the first real shipment.
		PartNum key, CSV-string echo fields, zero-padded UPCcode — and NO
		ack_qty in the output (the poll carries the placement's ack forward)."""
		r = normalize_order_status({
			"Status": "OK", "WebRef": "1000001", "PONum": "TEST-20260818-03",
			"HoldStatus": "FDS Hold",
			"Items": [{"Cost": "$527.50", "NFA": "N", "UPCcode": "0787450948723",
				"Invoiced": 1, "InvoiceQty": "", "Invoices": "INV-1,INV-2",
				"DateShipped": "2026-08-18", "TrackingNum": "1Z999",
				"SerialNum": "SN1,SN2", "PartNum": "CAHG8328-N",
				"ASNQty": "-1", "Warehouse": ""}],
			"StatusCode": "00", "StatusMssg": "Ran successfully"})
		self.assertEqual(r["po_num"], "TEST-20260818-03")
		self.assertEqual(r["web_ref"], "1000001")
		self.assertEqual(r["hold_status"], "FDS Hold")
		line = r["lines"][0]
		self.assertEqual(line["sku"], "CAHG8328-N")
		self.assertEqual(line["invoiced_qty"], 1)
		self.assertEqual(line["serials"], ["SN1", "SN2"])
		self.assertEqual(line["invoices"], ["INV-1", "INV-2"])
		self.assertEqual(line["tracking_numbers"], ["1Z999"])
		self.assertNotIn("ack_qty", line)

	def test_invoiced_qty_prefers_numeric_invoiced_else_sums_invoiceqty(self):
		"""A numeric Invoiced wins; a non-numeric one falls back to summing the
		InvoiceQty per-invoice CSV; garbage in both parses to 0, never a raise
		mid-poll."""
		from ffl_integrations.distributor.rsr_provider import _invoiced_qty

		self.assertEqual(_invoiced_qty({"Invoiced": 3, "InvoiceQty": "9"}), 3)
		self.assertEqual(_invoiced_qty({"Invoiced": "2", "InvoiceQty": ""}), 2)
		self.assertEqual(_invoiced_qty({"Invoiced": "Y", "InvoiceQty": "1,2"}), 3)
		self.assertEqual(_invoiced_qty({"Invoiced": "", "InvoiceQty": "4"}), 4)
		self.assertEqual(_invoiced_qty({"Invoiced": "Y", "InvoiceQty": "N/A"}), 0)
		self.assertEqual(_invoiced_qty({}), 0)


class TestShortShipAfterAckCarryForward(unittest.TestCase):
	"""MAJOR-1 regression net: with ack carried forward (check-order has no
	AckQty), the short-ship test must run on what actually SHIPPED — folded
	through compute_do_updates, never hand-rolled line dicts."""

	def _fold(self, wish, ack, status_row):
		lines = [_do_line("A", wish_qty=wish, ack_qty=ack)]
		status = normalize_order_status({"Items": [dict({"PartNum": "A"}, **status_row)]})
		res = tasks.compute_do_updates(lines, status)
		merged = dict(lines[0])
		merged.update(res["lines"]["A"])
		return tasks.short_shipped_lines([merged])

	def test_killed_line_with_full_placement_ack_still_alerts(self):
		# Placement acked 2 (= wish); RSR later killed the line. ack carries
		# forward at 2, so an ack-based test would stay silent forever — the
		# customer paid for 2 and gets 0.
		short = self._fold(2, 2, {"ItemStatus": "Killed"})
		self.assertEqual(len(short), 1)
		self.assertEqual(short[0]["short_by"], 2)
		self.assertEqual(short[0]["shipped_qty"], 0)

	def test_partial_shipment_with_full_ack_alerts_the_difference(self):
		short = self._fold(2, 2, {"ItemStatus": "Killed", "Invoiced": 1})
		self.assertEqual(short[0]["short_by"], 1)

	def test_fully_shipped_line_never_alerts(self):
		short = self._fold(2, 2, {"Invoiced": 2})
		self.assertEqual(short, [])

	def test_acked_but_unshipped_line_is_not_yet_short(self):
		short = self._fold(2, 2, {"Invoiced": 0})
		self.assertEqual(short, [])


class TestCheckAvailability(unittest.TestCase):
	"""The pre-order availability gate against the LIVE check-catalog row
	shape: PartNum key, OnHand quantity, $-money, per-item StatusCode in-band."""

	def _provider(self, rows):
		p = RSRProvider()

		class FakeClient:
			def check_catalog(self, skus, account="dealer", ship_context=None):
				assert account == "dealer"
				return rows
		p._client_obj = FakeClient()
		return p

	def test_matches_on_partnum_and_reads_onhand_and_money(self):
		p = self._provider([{"PartNum": "MGMPI571BLK", "OnHand": "193719",
			"Price": "9.10", "StatusCode": "00", "StatusMssg": "In stock"}])
		out = p.check_availability([{"sku": "MGMPI571BLK", "qty": 5}])
		self.assertEqual(out[0]["qty"], 193719)
		self.assertEqual(out[0]["cost"], 9.10)
		self.assertTrue(out[0]["available"])

	def test_blocked_item_is_unavailable_no_matter_the_onhand(self):
		# per-item StatusCode 03 "Item is blocked." (live) — stale OnHand must lose
		p = self._provider([{"PartNum": "GLK19", "OnHand": "847",
			"Price": "$527.50", "StatusCode": "03", "StatusMssg": "Item is blocked."}])
		out = p.check_availability([{"sku": "GLK19", "qty": 1}])
		self.assertFalse(out[0]["available"])
		self.assertEqual(out[0]["cost"], 527.50)   # $-prefixed money still parses

	def test_not_carried_and_absent_rows_are_unavailable(self):
		p = self._provider([{"PartNum": "KNOWN", "RestrictedReason": "Item not carried by RSR",
			"StatusCode": "02", "StatusMssg": "Item not carried by RSR"}])
		out = p.check_availability([{"sku": "KNOWN", "qty": 1}, {"sku": "GHOST", "qty": 1}])
		self.assertFalse(out[0]["available"])   # 02 row, no OnHand at all
		self.assertFalse(out[1]["available"])   # response never mentioned it

	def test_insufficient_onhand_is_unavailable(self):
		p = self._provider([{"PartNum": "A", "OnHand": "3", "Price": "1.00",
			"StatusCode": "00"}])
		out = p.check_availability([{"sku": "A", "qty": 5}])
		self.assertFalse(out[0]["available"])
		self.assertEqual(out[0]["qty"], 3)


class TestVerifyTransferDealer(unittest.TestCase):
	"""Truthiness is the verdict: None unless on file AND accepting."""

	def _provider(self, resp):
		p = RSRProvider()

		class FakeClient:
			def dealer_accept_transfers(self, ffl, account="fulfillment"):
				return resp
		p._client_obj = FakeClient()
		return p

	def test_on_file_and_accepting_returns_dealer(self):
		out = self._provider({"FFL": "9-99-999-07-7E-17760", "AcceptTransfers": "Y",
			"on_file": True}).verify_transfer_dealer("9-99-999-07-7E-17760")
		self.assertTrue(out["accepts_transfers"])

	def test_not_on_file_returns_none(self):
		out = self._provider({"FFL": "X", "AcceptTransfers": "N",
			"on_file": False}).verify_transfer_dealer("X")
		self.assertIsNone(out)

	def test_on_file_but_refusing_returns_none(self):
		# The dealer exists but would refuse the box — a truthy return here
		# would let `if result:` ship into a refusal.
		out = self._provider({"FFL": "Y-1", "AcceptTransfers": "N",
			"on_file": True}).verify_transfer_dealer("Y-1")
		self.assertIsNone(out)
