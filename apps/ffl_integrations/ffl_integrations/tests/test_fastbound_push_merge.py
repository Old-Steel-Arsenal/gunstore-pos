"""FastBound `update_item` read-merge-write (post-acquisition single-gun push).

PUT /Items/{id} is a WHOLE-RECORD REPLACE, not a merge — sending only the
firearm identity block dropped the required ItemNumber / Acquire_Date /
AcquisitionType and FastBound 400'd every POS-wins correction (reconcile
pos-wins, the Serial No "Sync with FastBound" button, and any direct
push_serial_to_fastbound call — including the MCP tool). The fix: GET the
current item, overlay the caller's fields, PUT the merged body so the required
fields survive.

All HTTP is mocked at the `_request` boundary; no live FastBound.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from ffl_integrations.fastbound.client import (
    FastBoundClient,
    FastBoundConfig,
    FastBoundError,
)


def _client() -> FastBoundClient:
    cfg = FastBoundConfig(
        account_num="TEST",
        api_key="k",
        base_url="https://example.invalid",
        api_version="1.0",
        audit_user="u",
        timeout=5,
    )
    return FastBoundClient(cfg, session=MagicMock())


# A realistic GET /Items/{id} single-item body: the writable identity/commercial
# block PLUS the read-only / response-only fields FastBound echoes back.
def _current_item() -> dict:
    return {
        # writable, required — must survive into the PUT or FastBound 400s
        "itemNumber": 4211,
        "acquisitionType": "Purchase",
        "acquire_Date": "2026-01-05",
        # writable identity the caller may correct
        "serial": "SN123",
        "manufacturer": "OLD MFG",
        "importer": "OLD IMP",
        "model": "OLD MODEL",
        "caliber": "9mm",
        "type": "Pistol",
        # writable, not touched by the caller — must be preserved as-is
        "countryOfManufacture": "United States",
        "barrelLength": "4.5",
        "totalLength": "7.5",
        "condition": "New",
        "cost": "300.00",
        "price": "500.00",
        "mpn": "OLDMPN",
        "upc": "111111111111",
        "sku": "ITEM-OLD",
        "location": "Case 1",
        "doNotDispose": False,
        "externalId": "SN123",
        "acquire_PurchaseOrderNumber": "PO-1",
        "acquire_InvoiceNumber": "INV-1",
        "acquire_ShipmentTrackingNumber": "TRK-1",
        # read-only / response-only — must NOT be echoed back into the PUT
        "id": "95911e29-1111-2222-3333-444455556666",
        "status": {"name": "Available"},
        "disposed": False,
        "dispositionId": None,
        "dispositionExternalId": None,
        "dispositionType": None,
        "dispose_Date": None,
        "dispose_FirstName": None,
        "dispose_LastName": None,
        "ttsn": None,
        "otsn": None,
        "deleted": False,
        "isTheftLoss": False,
        "isDestroyed": False,
        "acquisitionContactId": "contact-uuid-9",
        "createUtc": "2026-01-05T10:00:00Z",
        "updateUtc": "2026-01-06T10:00:00Z",
        "quickCodeUrl": "https://cloud.fastbound.com/q/abc",
        "attachments": [],
        "notes": [],
    }


READONLY_KEYS = [
    "id", "status", "disposed", "dispositionId", "dispositionExternalId",
    "dispositionType", "dispose_Date", "dispose_FirstName", "dispose_LastName",
    "ttsn", "otsn", "deleted", "isTheftLoss", "isDestroyed",
    "acquisitionContactId", "createUtc", "updateUtc", "quickCodeUrl",
    "attachments", "notes",
]


class UpdateItemReadMergeWrite(FrappeTestCase):
    def _run(self, current, fields):
        """Drive update_item with only the HTTP boundary mocked. Returns the
        list of (method, path, json_body) calls so tests can inspect the PUT."""
        calls: list = []
        client = _client()

        def fake_request(method, path, *, params=None, json_body=None):
            calls.append((method, path, json_body))
            if method == "GET":
                return current
            if method == "PUT":
                return {}
            raise AssertionError(f"unexpected {method} {path}")

        with patch.object(client, "_request", side_effect=fake_request):
            client.update_item("95911e29-1111-2222-3333-444455556666", fields)
        return calls

    def _put_body(self, calls) -> dict:
        puts = [c for c in calls if c[0] == "PUT"]
        self.assertEqual(len(puts), 1, "expected exactly one PUT")
        return puts[0][2]

    def test_merges_required_fields_from_get_into_put(self):
        """The PUT body must carry the required fields the caller never sends
        (itemNumber / acquisitionType / acquire_Date), read back from the GET."""
        fields = {
            "manufacturer": "Colt", "model": "1911", "serial": "SN123",
            "caliber": "45ACP", "type": "Pistol", "sku": "ITEM-OLD",
            "upc": "222222222222", "mpn": "NEWMPN", "importer": "ACME IMP",
        }
        body = self._put_body(self._run(_current_item(), fields))
        # required fields sourced from the GET survive
        for key in ("itemNumber", "acquisitionType", "acquire_Date"):
            self.assertIn(key, body, f"required {key!r} missing from PUT body")
        self.assertEqual(body["itemNumber"], 4211)
        self.assertEqual(body["acquisitionType"], "Purchase")
        self.assertEqual(body["acquire_Date"], "2026-01-05")

    def test_caller_fields_override_existing_values(self):
        fields = {
            "manufacturer": "Colt", "model": "1911", "caliber": "45ACP",
            "importer": "ACME IMP", "type": "Pistol", "serial": "SN123",
        }
        body = self._put_body(self._run(_current_item(), fields))
        self.assertEqual(body["manufacturer"], "Colt")
        self.assertEqual(body["model"], "1911")
        self.assertEqual(body["caliber"], "45ACP")
        self.assertEqual(body["importer"], "ACME IMP")

    def test_readonly_fields_are_stripped_from_put(self):
        fields = {"manufacturer": "Colt", "serial": "SN123"}
        body = self._put_body(self._run(_current_item(), fields))
        for key in READONLY_KEYS:
            self.assertNotIn(key, body, f"read-only {key!r} must not be PUT back")

    def test_untouched_writable_fields_are_preserved(self):
        fields = {"manufacturer": "Colt", "serial": "SN123"}
        body = self._put_body(self._run(_current_item(), fields))
        self.assertEqual(body["countryOfManufacture"], "United States")
        self.assertEqual(body["cost"], "300.00")
        self.assertEqual(body["price"], "500.00")
        self.assertEqual(body["location"], "Case 1")
        self.assertEqual(body["acquire_PurchaseOrderNumber"], "PO-1")
        self.assertEqual(body["externalId"], "SN123")

    def test_none_input_does_not_clobber_existing_fb_value(self):
        """_firearm_item_fields sends upc/mpn/sku as None when POS has no value.
        A correction push must not NULL out FastBound's existing identifiers."""
        fields = {"manufacturer": "Colt", "serial": "SN123",
                  "upc": None, "mpn": None, "sku": None}
        body = self._put_body(self._run(_current_item(), fields))
        self.assertEqual(body["upc"], "111111111111")
        self.assertEqual(body["mpn"], "OLDMPN")
        self.assertEqual(body["sku"], "ITEM-OLD")

    def test_get_404_raises_and_does_not_put(self):
        """Item gone in FastBound → surface a clear error, never a silent PUT of a
        replacement record."""
        calls: list = []
        client = _client()

        def fake_request(method, path, *, params=None, json_body=None):
            calls.append((method, path))
            if method == "GET":
                raise FastBoundError("GET Items/x → 404: not found", status_code=404)
            raise AssertionError("PUT must not run when the item is missing")

        with patch.object(client, "_request", side_effect=fake_request):
            with self.assertRaises(FastBoundError) as ctx:
                client.update_item("missing-id", {"manufacturer": "Colt"})

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertNotIn("PUT", [c[0] for c in calls])
