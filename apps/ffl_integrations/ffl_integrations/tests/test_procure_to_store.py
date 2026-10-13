"""Procure-to-store (brand back to the store) pure logic.

The bench-only wiring (receive_from_do → auto-hang → dispose, the doc reads and
the soft-reserve writes) lives in test_procure_to_store_integration.py
(FrappeTestCase, `make test`). This file is the PURE half — the receive-batch
serial extraction, the completion-gate generalisation, and the router mode
fallback — run in the stubbed-frappe harness.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ffl_integrations.distributor import receive_bridge as rb
from ffl_integrations.distributor import router
from ffl_integrations.distributor import tasks


def _rline(item_code, is_firearm=False, receive_serials=None, **kw):
	base = {
		"item_code": item_code,
		"sku": item_code,
		"is_firearm": is_firearm,
		"receive_qty": len(receive_serials or []) or 1,
		"receive_serials": list(receive_serials or []),
	}
	base.update(kw)
	return base


class TestProcuredSerialsByItem(unittest.TestCase):
	"""receive_bridge.procured_serials_by_item — the real Serial No names a
	procure-to-store batch just booked, grouped by item_code so the waiting web
	order's to_store rows can be filled with them (auto-hang)."""

	def test_firearm_line_serials_are_grouped_by_item_code(self):
		receivable = [_rline("GLK19", is_firearm=True, receive_serials=["SN1", "SN2"])]
		self.assertEqual(
			rb.procured_serials_by_item(receivable), {"GLK19": ["SN1", "SN2"]})

	def test_accessory_line_contributes_no_serials(self):
		# A to_store accessory has no serials — it is not auto-hung, just marked
		# arrived. Only firearm lines yield serials for the web order's serial rows.
		receivable = [_rline("HOLSTER", is_firearm=False, receive_serials=[])]
		self.assertEqual(rb.procured_serials_by_item(receivable), {})

	def test_a_non_firearm_line_with_stray_serials_is_excluded(self):
		# Defensive: only a FIREARM line's serials become a hang pool. A non-firearm
		# line that somehow carries serials (a data anomaly) must not seed the pool —
		# an accessory has no serial row to fill, and its serial is not a bound-book
		# unit.
		receivable = [_rline("HOLSTER", is_firearm=False, receive_serials=["X1"])]
		self.assertEqual(rb.procured_serials_by_item(receivable), {})

	def test_two_lines_sharing_an_item_code_merge_their_serials(self):
		receivable = [
			_rline("GLK19", is_firearm=True, receive_serials=["SN1"]),
			_rline("GLK19", is_firearm=True, receive_serials=["SN2"]),
		]
		self.assertEqual(
			rb.procured_serials_by_item(receivable), {"GLK19": ["SN1", "SN2"]})

	def test_empty_batch_is_empty(self):
		self.assertEqual(rb.procured_serials_by_item([]), {})


class TestAccessoryModeFallback(unittest.TestCase):
	"""router.accessory_mode — resolve a to_store/Direct mode for an accessory web
	line. A materialised Distributor Listing.fulfillment_mode wins; a blank one
	falls back to the brand-policy chain (brand table -> RSR per-item flag -> default Direct),
	so a blocked-brand accessory (Holosun) routes To Store instead of defaulting to
	a direct-to-buyer shipment RSR would refuse."""

	def test_a_materialised_listing_mode_wins(self):
		self.assertEqual(
			router.accessory_mode("To Store", brand_policy=None), "To Store")
		self.assertEqual(
			router.accessory_mode("Direct", brand_policy="To Store"), "Direct")

	def test_blank_mode_falls_back_to_a_to_store_brand_policy(self):
		self.assertEqual(
			router.accessory_mode("", brand_policy="To Store"), "To Store")

	def test_blank_mode_falls_back_to_the_block_flags(self):
		# No brand entry, but RSR won't drop-ship it → To Store (physical constraint).
		self.assertEqual(
			router.accessory_mode(None, dropship_blocked=True), "To Store")
		self.assertEqual(
			router.accessory_mode(None, vendor_approval_required=True), "To Store")

	def test_blank_mode_with_no_signal_defaults_direct(self):
		self.assertEqual(router.accessory_mode(""), "Direct")


def _leg(name, status="Complete", order_type="Procure To Store"):
	return {"name": name, "status": status, "order_type": order_type}


