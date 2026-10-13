"""ShipStation shipment void — the Cancel Order integration leg.
HTTP + frappe.db are mocked; never hits the live API."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.shipstation import api
from ffl_integrations.shipstation.client import ShipStationClient, ShipStationConfig


def _client() -> ShipStationClient:
	cfg = ShipStationConfig(api_key="k", base_url="https://example.invalid/v2",
		store_id="", ship_from_warehouse_id="wh-1", default_parcel_weight_oz=16,
		timeout=5, sandbox_mode=False)
	return ShipStationClient(cfg, session=MagicMock())


class CancelShipmentClient(FrappeTestCase):
	def test_cancel_is_a_put(self):
		"""PUT, never POST. This assertion said POST until 2026-09-11 — it had
		been written from the implementation instead of from the contract, so
		it certified the bug: the endpoint answers a POST with 405, the void
		leg of Cancel Order had never once worked, and the first operator to
		cancel a pushed order left a live ShipStation
		shipment behind a cancelled invoice. If this fails, read the contract
		(ShipEngine operationId `cancel_shipments`) before touching it — the
		whole verb surface is pinned in test_shipstation_client's wire
		contract table."""
		client = _client()
		with patch.object(client, "_request", return_value=None) as req:
			client.cancel_shipment("se-352522551")
		req.assert_called_once_with("PUT", "shipments/se-352522551/cancel")


class VoidShipment(FrappeTestCase):
	def _void(self, fields, client=None):
		client = client or _client()
		with patch.object(ShipStationClient, "from_settings", return_value=client), \
				patch("frappe.db.get_value", return_value=fields), \
				patch("frappe.db.set_value") as set_value:
			out = api.void_shipment("ACC-SINV-TEST-1")
		return out, set_value

	def test_voids_pushed_shipment_and_stamps_cancelled(self):
		client = _client()
		with patch.object(client, "cancel_shipment") as cancel:
			out, set_value = self._void(frappe._dict(
				shipstation_order_id="se-1", shipstation_status="Pending"), client)
		cancel.assert_called_once_with("se-1")
		self.assertTrue(out["voided"])
		stamped = [c for c in set_value.call_args_list
			if c.args[:2] == ("Sales Invoice", "ACC-SINV-TEST-1")
			and c.args[2] == "shipstation_status" and c.args[3] == "Cancelled"]
		self.assertTrue(stamped)

	def test_never_pushed_is_skipped(self):
		out, set_value = self._void(frappe._dict(
			shipstation_order_id="", shipstation_status="Not Requested"))
		self.assertEqual(out.get("skipped"), "never pushed")
		set_value.assert_not_called()

	def test_already_cancelled_is_noop(self):
		client = _client()
		with patch.object(client, "cancel_shipment") as cancel:
			out, _ = self._void(frappe._dict(
				shipstation_order_id="se-1", shipstation_status="Cancelled"), client)
		cancel.assert_not_called()
		self.assertTrue(out.get("already"))


if __name__ == "__main__":
	unittest.main()
