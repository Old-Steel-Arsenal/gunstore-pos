"""FastBound disposition DELETE — orphan-item verification.

FastBound's DELETE /Dispositions/{id} removes the disposition record but can
leave the item status=Disposed with a dangling dispositionId (observed in
prod 2026-07-07: DISP-2026-00986 stayed Disposed after a successful DELETE).
delete_disposition_from_fastbound must verify the item landed back in
inventory and undispose it when needed. HTTP + frappe.db are mocked — no
live calls, no DB writes.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from ffl_integrations.fastbound import tasks
from ffl_integrations.fastbound.client import (
	FastBoundClient,
	FastBoundConfig,
	FastBoundError,
)


def _client() -> FastBoundClient:
	cfg = FastBoundConfig(account_num="TEST", api_key="k", base_url="https://example.invalid",
		api_version="1.0", audit_user="u", timeout=5)
	return FastBoundClient(cfg, session=MagicMock())


DISPOSED_ITEM = {"id": "fbitem-1", "status": {"name": "Disposed"}, "dispositionId": "fb-gone"}
AVAILABLE_ITEM = {"id": "fbitem-1", "status": {"name": "Available"}, "dispositionId": None}


def _db_get_value(doctype, name, fieldname, *args, **kwargs):
	if doctype == "FFL Disposition" and fieldname == "serial":
		return "SN-75314"
	if doctype == "Serial No" and fieldname == "fastbound_item_id":
		return "fbitem-1"
	return None


class DeleteVerifiesUndisposed(FrappeTestCase):
	def _run(self, client):
		with patch.object(FastBoundClient, "from_settings", return_value=client), \
				patch("frappe.db.get_value", side_effect=_db_get_value), \
				patch("frappe.db.set_value") as set_value:
			tasks.delete_disposition_from_fastbound(
				"DISP-T-1", "fb-gone", _retries=0, _commit=False)
		return set_value

	def test_orphaned_item_is_undisposed(self):
		client = _client()
		with patch.object(client, "delete_disposition") as delete, \
				patch.object(client, "get_item", return_value=dict(DISPOSED_ITEM)), \
				patch.object(client, "undispose_item") as undo:
			set_value = self._run(client)
		delete.assert_called_once_with("fb-gone")
		undo.assert_called_once()
		self.assertEqual(undo.call_args.args[0], "fbitem-1")
		cleared = [c for c in set_value.call_args_list
			if c.args[:2] == ("FFL Disposition", "DISP-T-1")
			and isinstance(c.args[2], dict) and c.args[2].get("fastbound_id") == ""]
		self.assertTrue(cleared, "success path must still clear fastbound_id")

	def test_already_available_item_is_left_alone(self):
		client = _client()
		with patch.object(client, "delete_disposition"), \
				patch.object(client, "get_item", return_value=dict(AVAILABLE_ITEM)), \
				patch.object(client, "undispose_item") as undo:
			self._run(client)
		undo.assert_not_called()

	def test_undispose_failure_marks_sync_error(self):
		client = _client()
		with patch.object(client, "delete_disposition"), \
				patch.object(client, "get_item", return_value=dict(DISPOSED_ITEM)), \
				patch.object(client, "undispose_item", side_effect=FastBoundError("boom")):
			set_value = self._run(client)
		marked = [c for c in set_value.call_args_list
			if c.args[:2] == ("FFL Disposition", "DISP-T-1")
			and c.args[2] == "fastbound_sync_error"
			and str(c.args[3]).startswith("DELETE failed:")]
		self.assertTrue(marked, "undispose failure must keep the 'DELETE failed:' "
			"marker prefix that reconcile_failed_deletes() retries")

	def test_missing_serial_skips_verification(self):
		client = _client()
		with patch.object(client, "delete_disposition"), \
				patch.object(client, "get_item") as get_item, \
				patch.object(client, "undispose_item") as undo, \
				patch.object(FastBoundClient, "from_settings", return_value=client), \
				patch("frappe.db.get_value", return_value=None), \
				patch("frappe.db.set_value"):
			tasks.delete_disposition_from_fastbound(
				"DISP-T-1", "fb-gone", _retries=0, _commit=False)
		get_item.assert_not_called()
		undo.assert_not_called()


class UndisposeItemClient(FrappeTestCase):
	def test_puts_undispose_with_note(self):
		client = _client()
		with patch.object(client, "_request", return_value=None) as req:
			client.undispose_item("fbitem-1", note="POS cancel")
		req.assert_called_once_with("PUT", "Items/fbitem-1/Undispose",
			json_body={"note": "POS cancel"})


if __name__ == "__main__":
	unittest.main()
