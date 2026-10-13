"""Ingestion: _process_lines builds per-gun serial rows and holds stock."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase


def _order():
	doc = MagicMock()
	doc.name = None
	doc.lines = []
	doc.serials = []
	def _append(table, row):
		getattr(doc, table).append(frappe._dict(row))
		return frappe._dict(row)
	doc.append.side_effect = _append
	return doc


def setUpModule():
	# These tests stub frappe.db.get_value for the whole test. A Meta built under that stub is built out of
	# the fixture, so build the real one first. Alone in a fresh process nothing else has (docs/testing.md).
	from ffl_core.tests.channel_query_doubles import warm_channel_meta

	warm_channel_meta()


class TestProcessLinesSerialRows(FrappeTestCase):
	def test_uniform_firearm_qty2_makes_two_placeholder_rows(self):
		from ffl_woo_sync.woocommerce import webhook
		body_lines = [{"id": 7, "sku": "GLOCK-19", "quantity": 2, "price": "500",
			"subtotal": "1000", "total": "1000", "total_tax": "0", "meta_data": []}]
		order = _order()
		with patch.object(webhook, "is_regulated_firearm_item", return_value=True), \
			 patch.object(webhook, "is_firearm_item", return_value=True), \
			 patch.object(webhook, "_pick_oldest_unreserved_serial",
				 side_effect=["SN-A", "SN-B"]) as mock_pick:
			to_reserve = webhook._process_lines(order, body_lines)
		self.assertEqual(len(order.lines), 1)
		self.assertEqual(order.lines[0]["serial"], "")
		self.assertEqual(len(order.serials), 2)
		self.assertEqual([r["unit_index"] for r in order.serials], [0, 1])
		self.assertTrue(all(r["confirmed_serial"] == "" for r in order.serials))
		self.assertEqual([r["stock_hold_serial"] for r in order.serials], ["SN-A", "SN-B"])
		self.assertEqual(to_reserve, ["SN-A", "SN-B"])
		self.assertEqual(mock_pick.call_count, 2)

	def test_per_gun_sku_prefilled_serial_is_confirmed(self):
		from ffl_woo_sync.woocommerce import webhook
		body_lines = [{"id": 9, "sku": "AR15::SN-XYZ", "quantity": 1, "price": "900",
			"meta_data": [{"key": "firearm_serials", "value": ["SN-XYZ"]}]}]
		order = _order()
		with patch.object(webhook, "is_regulated_firearm_item", return_value=True), \
			 patch.object(webhook, "is_firearm_item", return_value=True), \
			 patch.object(webhook, "_serial_availability", return_value=("", "")), \
			 patch.object(webhook, "_pick_oldest_unreserved_serial") as mock_pick:
			to_reserve = webhook._process_lines(order, body_lines)
		self.assertEqual(order.lines[0]["serial"], "")  # vestigial column stays blank
		self.assertEqual(len(order.serials), 1)
		self.assertEqual(order.serials[0]["confirmed_serial"], "SN-XYZ")
		self.assertEqual(order.serials[0]["stock_hold_serial"], "")
		self.assertEqual(to_reserve, ["SN-XYZ"])
		mock_pick.assert_not_called()

	def test_no_available_placeholder_records_reservation_error(self):
		from ffl_woo_sync.woocommerce import webhook
		body_lines = [{"id": 7, "sku": "GLOCK-19", "quantity": 1, "meta_data": []}]
		order = _order()
		with patch.object(webhook, "is_regulated_firearm_item", return_value=True), \
			 patch.object(webhook, "is_firearm_item", return_value=True), \
			 patch.object(webhook, "_pick_oldest_unreserved_serial", return_value=None), \
			 patch.object(webhook, "_log_error"):
			to_reserve = webhook._process_lines(order, body_lines)
		self.assertEqual(to_reserve, [])
		self.assertEqual(order.serials[0]["confirmed_serial"], "")
		self.assertIn("GLOCK-19", order.serials[0]["reservation_error"])

	def test_invalid_field_serial_records_error_no_reserve(self):
		from ffl_woo_sync.woocommerce import webhook
		body_lines = [{"id": 7, "sku": "GLOCK-19", "quantity": 1,
			"meta_data": [{"key": "firearm_serials", "value": ["SN-GHOST"]}]}]
		order = _order()
		with patch.object(webhook, "is_regulated_firearm_item", return_value=True), \
			 patch.object(webhook, "is_firearm_item", return_value=True), \
			 patch.object(webhook, "_serial_availability", return_value=(
				 "Serial 'SN-GHOST' is not a valid in-stock serial for 'GLOCK-19'", "")), \
			 patch.object(webhook, "_pick_oldest_unreserved_serial") as mock_pick, \
			 patch.object(webhook, "_log_error"):
			to_reserve = webhook._process_lines(order, body_lines)
		self.assertEqual(to_reserve, [])
		self.assertEqual(order.serials[0]["confirmed_serial"], "")
		self.assertIn("SN-GHOST", order.serials[0]["reservation_error"])
		mock_pick.assert_not_called()

	def test_non_firearm_line_makes_no_serial_rows(self):
		from ffl_woo_sync.woocommerce import webhook
		body_lines = [{"id": 3, "sku": "AMMO-9MM", "quantity": 2, "meta_data": []}]
		order = _order()
		with patch.object(webhook, "is_regulated_firearm_item", return_value=False), \
			 patch.object(webhook, "is_firearm_item", return_value=False):
			to_reserve = webhook._process_lines(order, body_lines)
		self.assertEqual(len(order.lines), 1)
		self.assertEqual(order.serials, [])
		self.assertEqual(to_reserve, [])


class TestSerialUnavailableReason(FrappeTestCase):
	"""Direct coverage of the lock-and-verify guard. The authoritative 'held'
	flag is ``woo_reserved`` alone (matching ``_pick_oldest_unreserved_serial``
	and the stock model) — a residual reservation with a blank ``woo_order``
	must still be rejected, else two webhooks double-book the same gun."""

	def test_free_serial_returns_empty(self):
		from ffl_woo_sync.woocommerce.webhook import _serial_unavailable_reason
		with patch("frappe.db.get_value", return_value={
				"item_code": "GLOCK-19", "status": "Active",
				"woo_reserved": 0, "woo_order": ""}):
			self.assertEqual(_serial_unavailable_reason("SN-1", "GLOCK-19"), "")

	def test_missing_serial_returns_reason(self):
		from ffl_woo_sync.woocommerce.webhook import _serial_unavailable_reason
		with patch("frappe.db.get_value", return_value=None):
			reason = _serial_unavailable_reason("SN-GHOST", "GLOCK-19")
		self.assertTrue(reason)
		self.assertIn("SN-GHOST", reason)

	def test_wrong_item_returns_reason(self):
		from ffl_woo_sync.woocommerce.webhook import _serial_unavailable_reason
		with patch("frappe.db.get_value", return_value={
				"item_code": "OTHER-ITEM", "status": "Active",
				"woo_reserved": 0, "woo_order": ""}):
			reason = _serial_unavailable_reason("SN-1", "GLOCK-19")
		self.assertTrue(reason)

	def test_inactive_serial_returns_reason(self):
		from ffl_woo_sync.woocommerce.webhook import _serial_unavailable_reason
		with patch("frappe.db.get_value", return_value={
				"item_code": "GLOCK-19", "status": "Delivered",
				"woo_reserved": 0, "woo_order": ""}):
			reason = _serial_unavailable_reason("SN-1", "GLOCK-19")
		self.assertTrue(reason)

	def test_reserved_with_blank_order_returns_reason(self):
		"""The bug case: woo_reserved=1 but woo_order blank (a residual /
		half-written reservation) must still be treated as held."""
		from ffl_woo_sync.woocommerce.webhook import _serial_unavailable_reason
		with patch("frappe.db.get_value", return_value={
				"item_code": "GLOCK-19", "status": "Active",
				"woo_reserved": 1, "woo_order": ""}):
			reason = _serial_unavailable_reason("SN-1", "GLOCK-19")
		self.assertTrue(reason)

	def test_reserved_by_other_order_returns_reason_naming_it(self):
		from ffl_woo_sync.woocommerce.webhook import _serial_unavailable_reason
		with patch("frappe.db.get_value", return_value={
				"item_code": "GLOCK-19", "status": "Active",
				"woo_reserved": 1, "woo_order": "WOO-ORD-OTHER"}):
			reason = _serial_unavailable_reason("SN-1", "GLOCK-19")
		self.assertTrue(reason)
		self.assertIn("WOO-ORD-OTHER", reason)
