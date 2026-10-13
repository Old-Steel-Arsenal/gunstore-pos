"""FastBound disposition.committed → take a directly-disposed gun out of stock.

Tests the guard logic of _sync_fb_direct_disposal_stock: it matches disposed guns
by the stable FastBound item id (fastbound_item_id) — never the serial string —
and issues a still-in-stock firearm out of ERPNext when FastBound reports it
disposed, while skipping guns our own POS path owns (open POS Invoice) or that are
already gone / unmatched. So a retail sale we're processing is never
double-decremented, and we never take the WRONG gun out on a serial collision."""
from __future__ import annotations

from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase


# Event carries the FB item id (+ serial); matching keys off the id, not the serial.
_DATA = {"items": [{"id": "FB-IT-1", "serial": "FBX-SYNC-1"}]}
_TARGET = {"serial": "FBX-SYNC-1", "item_code": "GUN-X", "warehouse": "WH-1", "fb_id": "FB-IT-1"}


class TestFbDirectDisposalStockSync(FrappeTestCase):
	@patch("ffl_core.firearm.issue_serial")
	@patch("ffl_integrations.fastbound.webhook._serial_on_open_pos_invoice", return_value=False)
	@patch("ffl_integrations.fastbound.inventory_sync.in_stock_targets", return_value=[_TARGET])
	def test_issues_a_fb_direct_disposal(self, _targets, _onpos, mock_issue):
		from ffl_integrations.fastbound import webhook as fb
		fb._sync_fb_direct_disposal_stock("FB-DISP-9", _DATA)
		mock_issue.assert_called_once()
		args = mock_issue.call_args.args
		self.assertEqual(args[0], "GUN-X")       # item_code
		self.assertEqual(args[1], "FBX-SYNC-1")  # serial
		self.assertEqual(args[2], "WH-1")        # warehouse

	@patch("ffl_core.firearm.issue_serial")
	@patch("ffl_integrations.fastbound.webhook._serial_on_open_pos_invoice", return_value=True)
	@patch("ffl_integrations.fastbound.inventory_sync.in_stock_targets", return_value=[_TARGET])
	def test_skips_when_serial_is_on_an_open_pos_invoice(self, _targets, _onpos, mock_issue):
		# Our POS sale owns the stock-out — the disposal webhook must not race it.
		from ffl_integrations.fastbound import webhook as fb
		fb._sync_fb_direct_disposal_stock("FB-DISP-9", _DATA)
		mock_issue.assert_not_called()

	@patch("ffl_core.firearm.issue_serial")
	@patch("ffl_integrations.fastbound.webhook._serial_on_open_pos_invoice", return_value=False)
	@patch("ffl_integrations.fastbound.inventory_sync.in_stock_targets", return_value=[])
	def test_skips_when_no_in_stock_target(self, _targets, _onpos, mock_issue):
		# in_stock_targets returns [] when the gun is already out of stock, isn't a
		# firearm, or has no Serial No with a matching fastbound_item_id — idempotent
		# no-op + the safe "under-sync" direction for an unmatched serial.
		from ffl_integrations.fastbound import webhook as fb
		fb._sync_fb_direct_disposal_stock("FB-DISP-9", _DATA)
		mock_issue.assert_not_called()

	@patch("ffl_core.firearm.issue_serial")
	@patch("ffl_integrations.fastbound.webhook._fetch_disposition_fb_items", return_value={})
	@patch("ffl_integrations.fastbound.inventory_sync.in_stock_targets")
	def test_skips_when_no_fb_item_ids(self, mock_targets, _fetch, mock_issue):
		# Event items carry a serial but NO FastBound id, and the fetch finds none →
		# we never fall back to a serial-string match (the whole point of the fix).
		from ffl_integrations.fastbound import webhook as fb
		fb._sync_fb_direct_disposal_stock("FB-DISP-9", {"items": [{"serial": "FBX-SYNC-1"}]})
		mock_targets.assert_not_called()
		mock_issue.assert_not_called()

	@patch("ffl_core.firearm.issue_serial")
	@patch("ffl_integrations.fastbound.webhook._serial_on_open_pos_invoice", return_value=False)
	@patch("ffl_integrations.fastbound.inventory_sync.in_stock_targets", return_value=[_TARGET])
	@patch("ffl_integrations.fastbound.webhook._fetch_disposition_fb_items",
		return_value={"FB-IT-1": "FBX-SYNC-1"})
	def test_falls_back_to_disposition_fetch_when_event_has_no_items(
			self, mock_fetch, mock_targets, _onpos, mock_issue):
		from ffl_integrations.fastbound import webhook as fb
		fb._sync_fb_direct_disposal_stock("FB-DISP-9", {})  # no items in the payload
		# is_4473=False here (payload has no is4473 flag) → the GET fallback still runs
		# for a genuinely non-4473 direct disposal (P3 leaves non-4473 unchanged).
		mock_fetch.assert_called_once_with("FB-DISP-9", is_4473=False)
		# in_stock_targets is matched against the fetched {fb_id: serial} map.
		self.assertEqual(mock_targets.call_args.args[0], {"FB-IT-1": "FBX-SYNC-1"})
		mock_issue.assert_called_once()


class TestFbIdSerialMap(FrappeTestCase):
	"""The id/serial extractor underpinning the fastbound_item_id match."""

	def test_extracts_flat_nested_and_skips_missing_id(self):
		from ffl_integrations.fastbound.webhook import _fb_id_serial_map
		items = [
			{"id": "A", "serial": "SER-A"},               # flat ItemModel
			{"item": {"id": "B", "serial": "SER-B"}},      # nested DispositionItemModel
			{"serial": "SER-C"},                           # no id → skipped (can't match safely)
			"not-a-dict",
		]
		self.assertEqual(_fb_id_serial_map(items), {"A": "SER-A", "B": "SER-B"})
		self.assertEqual(_fb_id_serial_map(None), {})


if __name__ == "__main__":
	import unittest
	unittest.main()
