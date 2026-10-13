"""FastBound client HTTP plumbing — pagination semantics.

FastBound's list endpoints document `skip` as "how many pages to skip" (a page
index), NOT a record offset — confirmed against the live account: with take=2,
skip=1 returns items 2-3, not 1-2. The paginators must advance `skip` by one
page per request, or they silently read only the first page.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.fastbound.client import FastBoundClient, FastBoundConfig


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


class FastBoundAcquisitionContact(FrappeTestCase):
	"""_acquisition_payload's seller-contact block: structured name + suffix +
	parsed address + stable externalId. Regression for a trade-in
	whose contact went out as a split guess ("pat" / "lee IV
	Sampleton") with 'Unknown - please edit in FastBound' placeholders."""

	def _acq(self, **over):
		base = dict(
			name="ACQ-CONTACT-TEST", acquisition_date="2026-07-17T12:00:00",
			source_type="Trade-in", source_name="pat lee IV Sampleton",
			source_first_name="Pat", source_middle_name="Lee",
			source_last_name="Sampleton", source_suffix="IV",
			source_address="500 Elm St, Springfield TX 75024",
			source_customer="pat lee IV Sampleton",
			source_ffl=None, item="_FB_NO_SUCH_ITEM", serial="605925-TEST",
			cost=639, notes="", receive_source="", transaction_type="",
			amended_from=None,
			company=frappe.db.get_single_value("Global Defaults", "default_company"),
		)
		base.update(over)
		return frappe._dict(base)

	def test_individual_contact_carries_structured_name_suffix_address(self):
		body = FastBoundClient._acquisition_payload(self._acq())
		c = body["contact"]
		self.assertEqual(c["firstName"], "Pat")
		self.assertEqual(c["middleName"], "Lee")
		self.assertEqual(c["lastName"], "Sampleton")
		self.assertEqual(c["suffix"], "IV")
		self.assertEqual(c["premiseAddress1"], "500 Elm St")
		self.assertEqual(c["premiseCity"], "Springfield")
		self.assertEqual(c["premiseState"], "TX")
		self.assertEqual(c["premiseZipCode"], "75024")
		self.assertEqual(c["externalId"], "customer-pat lee IV Sampleton")

	def test_suffix_omitted_when_blank(self):
		body = FastBoundClient._acquisition_payload(self._acq(source_suffix=""))
		self.assertNotIn("suffix", body["contact"])

	def test_blank_address_falls_back_to_customer_primary_address(self):
		"""Receive Goods Individual captures no free-text
		address — the push must fall back to the linked Customer's primary
		Address instead of booking 'Unknown' placeholders."""
		cust = "_FB Fallback Seller"
		if not frappe.db.exists("Customer", cust):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": cust,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		addr = frappe.get_doc({
			"doctype": "Address", "address_title": cust, "address_type": "Billing",
			"address_line1": "77 Fallback Rd", "city": "Springfield", "state": "TX",
			"pincode": "75024", "country": "United States",
			"links": [{"link_doctype": "Customer", "link_name": cust}],
		}).insert(ignore_permissions=True)
		frappe.db.set_value("Customer", cust, "customer_primary_address", addr.name)

		body = FastBoundClient._acquisition_payload(
			self._acq(source_address="", source_customer=cust))
		c = body["contact"]
		self.assertEqual(c["premiseAddress1"], "77 Fallback Rd")
		self.assertEqual(c["premiseCity"], "Springfield")
		self.assertEqual(c["premiseState"], "TX")
		self.assertEqual(c["premiseZipCode"], "75024")

	def test_blank_address_without_customer_keeps_placeholder(self):
		body = FastBoundClient._acquisition_payload(
			self._acq(source_address="", source_customer=""))
		self.assertEqual(body["contact"]["premiseCity"], "Unknown")

	def test_placeholder_state_is_the_acquisitions_own_company_address_state(self):
		from unittest.mock import patch

		addr = frappe.get_doc({
			"doctype": "Address", "address_title": "_FB Placeholder Home",
			"address_type": "Billing", "address_line1": "100 Main St",
			"city": "Springfield", "state": "Nevada", "pincode": "89000",
			"country": "United States"}).insert(ignore_permissions=True)
		seen = []

		def company_address(company):
			seen.append(company)
			return addr.name if company == "_FB Other Co" else None

		blank = self._acq(source_address="", source_customer="", company="_FB Other Co")
		frappe.local.request_cache.clear()
		with patch("ffl_core.store_identity.company_address", side_effect=company_address):
			c = FastBoundClient._acquisition_payload(blank)["contact"]
		self.assertEqual(c["premiseState"], "NV")
		# The acquisition's own Company was asked, not the default one.
		self.assertEqual(set(seen), {"_FB Other Co"})
		frappe.local.request_cache.clear()

	def test_placeholder_without_a_company_address_stops_with_the_fix_it_message(self):
		from unittest.mock import patch

		blank = self._acq(source_address="", source_customer="")
		frappe.local.request_cache.clear()
		with patch("ffl_core.store_identity.company_address", return_value=None):
			with self.assertRaises(frappe.ValidationError) as caught:
				FastBoundClient._acquisition_payload(blank)
		self.assertIn("Company › Addresses", str(caught.exception))
		frappe.local.request_cache.clear()

	def test_placeholder_without_any_company_is_a_clear_error_not_an_index_error(self):
		blank = self._acq(source_address="", source_customer="", company=None)
		with self.assertRaises(frappe.ValidationError) as caught:
			FastBoundClient._acquisition_payload(blank)
		self.assertIn("Set up a Company with an address", str(caught.exception))

	def test_a_parsed_address_needs_no_company(self):
		"""The state comes from the parsed address; the Company is only the
		placeholder's source."""
		c = FastBoundClient._acquisition_payload(self._acq(company=None))["contact"]
		self.assertEqual(c["premiseState"], "TX")

	def test_amendment_tags_predecessor_in_note(self):
		"""An amendment's bound-book entry must read as a
		CORRECTION — the note leads with the predecessor it amends, followed
		by the human reason stamped on the acquisition."""
		body = FastBoundClient._acquisition_payload(self._acq(
			amended_from="ACQ-OLD-1", notes="cost corrected 630 -> 639"))
		self.assertEqual(body["note"], "Amends ACQ-OLD-1 · cost corrected 630 -> 639")


class FastBoundPagination(FrappeTestCase):
    def test_iter_items_paginates_by_page_index(self):
        """250 items over page_size 100 → 3 data pages (skip 0,1,2), then a final
        empty skip=3 terminates (a short page isn't a reliable end signal)."""
        all_items = [{"serial": f"S{i}", "id": f"id{i}"} for i in range(250)]
        page_size = 100
        seen_skips: list[int] = []

        def fake_request(method, path, *, params=None, **_):
            skip, take = params["skip"], params["take"]
            seen_skips.append(skip)
            start = skip * take  # FastBound: skip is a PAGE index
            return {"items": all_items[start : start + take], "records": len(all_items)}

        client = _client()
        with patch.object(client, "_request", side_effect=fake_request):
            got = [item for page in client.iter_items(page_size=page_size) for item in page]

        self.assertEqual(len(got), 250)
        self.assertEqual([i["id"] for i in got], [i["id"] for i in all_items])
        self.assertEqual(seen_skips, [0, 1, 2, 3])

    def test_iter_items_short_page_then_empty_terminates(self):
        """A short page does NOT end iteration; the following empty page does — so
        a partial intermediate page can never silently truncate the bound book."""
        all_items = [{"serial": f"S{i}", "id": f"id{i}"} for i in range(9)]
        seen_skips: list[int] = []

        def fake_request(method, path, *, params=None, **_):
            skip, take = params["skip"], params["take"]
            seen_skips.append(skip)
            start = skip * take
            return {"items": all_items[start : start + take], "records": len(all_items)}

        client = _client()
        with patch.object(client, "_request", side_effect=fake_request):
            got = [item for page in client.iter_items(page_size=100) for item in page]

        self.assertEqual(len(got), 9)
        self.assertEqual(seen_skips, [0, 1])  # short page 0, then empty page 1 stops

    def test_iter_items_short_intermediate_page_not_truncated(self):
        """Regression: a non-full INTERMEDIATE page (FastBound can return one under
        load) must not stop iteration — the old `len < page_size` early-return
        silently dropped every later page."""
        pages = {0: [{"id": "a"}, {"id": "b"}], 1: [{"id": "c"}, {"id": "d"}], 2: []}
        seen_skips: list[int] = []

        def fake_request(method, path, *, params=None, **_):
            skip = params["skip"]
            seen_skips.append(skip)
            return {"items": pages.get(skip, []), "records": 4}

        client = _client()
        with patch.object(client, "_request", side_effect=fake_request):
            got = [item for page in client.iter_items(page_size=100) for item in page]

        self.assertEqual([i["id"] for i in got], ["a", "b", "c", "d"])  # nothing dropped
        self.assertEqual(seen_skips, [0, 1, 2])


class FastBoundSmartList(FrappeTestCase):
    def test_get_smartlist_parses_dedupes_and_strips(self):
        client = _client()
        with patch.object(client, "_request",
                          return_value={"smartList": ["Purchase", "Consignment", "Purchase", " ", "Custom Type"]}) as req:
            got = client.get_smartlist("AcquireType")
        req.assert_called_once_with("GET", "SmartLists/AcquireType")
        self.assertEqual(got, ["Purchase", "Consignment", "Custom Type"])

    def test_get_smartlist_empty_response(self):
        client = _client()
        with patch.object(client, "_request", return_value={}):
            self.assertEqual(client.get_smartlist("AcquireType"), [])

    def test_sync_smart_lists_caches_both(self):
        from ffl_integrations.fastbound import client_api
        # Ensure required creds exist so the single-doctype save() validates.
        s = frappe.get_single("FastBound Settings")
        s.account_num = s.account_num or "TEST-ACC"
        if not s.api_key:
            s.api_key = "test-key"
        s.flags.ignore_permissions = True
        s.save()

        lists = {"AcquireType": ["Purchase", "Trade"], "DisposeType": ["Sale", "Return"]}
        with patch.object(FastBoundClient, "from_settings", return_value=_client()), \
             patch.object(FastBoundClient, "get_smartlist", side_effect=lambda name: lists[name]):
            res = client_api.sync_smart_lists()

        self.assertEqual(res["acquire_count"], 2)
        self.assertEqual(res["dispose_count"], 2)
        self.assertEqual(client_api.get_acquire_types(), ["Purchase", "Trade"])
        self.assertEqual(client_api.get_dispose_types(), ["Sale", "Return"])


class FastBoundUpdateItemSerial(FrappeTestCase):
	"""update_item_serial — the FastBound half of a POS serial-number correction.
	Edits the bound-book item's serial in place (PUT /Items/{id}) and rides an
	audit note along on the same request. Read-merge-write must keep the other
	writable fields and must NOT drop the corrected serial back to the old one."""

	@staticmethod
	def _capture(current):
		"""side_effect for _request: GET returns the current item, PUT captures
		and returns the merged body sent to FastBound."""
		sent = {}

		def fake_request(method, path, *, params=None, json_body=None):
			if method == "GET":
				return dict(current)
			if method == "PUT":
				sent["body"] = json_body
				return {}
			raise AssertionError(f"unexpected {method} {path}")

		return fake_request, sent

	def test_update_item_serial_syncs_serial_and_externalid_and_note(self):
		# In this integration the item's externalId IS the serial (acquisition push
		# sets externalId=serial; GetByExternalId / reconcile resolve by it), so a
		# correction MUST move both together or FastBound<->POS diverge.
		current = {
			"itemNumber": "42", "serial": "OLD-123", "externalId": "OLD-123",
			"manufacturer": "Colt", "model": "1911", "caliber": ".45 ACP",
			"type": "Pistol", "acquisitionType": "Individual", "acquire_Date": "2026-04-01",
			# a response-only field FastBound merely echoes — must never ride into the PUT
			"disposed": False,
		}
		fake, sent = self._capture(current)
		client = _client()
		with patch.object(client, "_request", side_effect=fake):
			client.update_item_serial("fb-item-1", "NEW-456", note="Serial corrected OLD-123→NEW-456")
		body = sent["body"]
		# serial AND externalId both move to the corrected serial
		self.assertEqual(body["serial"], "NEW-456")
		self.assertEqual(body["externalId"], "NEW-456")
		self.assertEqual(body["note"], "Serial corrected OLD-123→NEW-456")
		# read-merge-write preserved the other required writable fields
		self.assertEqual(body["itemNumber"], "42")
		self.assertEqual(body["manufacturer"], "Colt")
		self.assertEqual(body["acquisitionType"], "Individual")
		# response-only echo did not ride back into the PUT
		self.assertNotIn("disposed", body)

	def test_update_item_serial_sets_externalid_when_blank(self):
		# A FastBound-direct item we adopted has a null externalId — set it to the
		# new serial so GetByExternalId can resolve the gun afterwards.
		fake, sent = self._capture({"serial": "OLD", "itemNumber": "1"})  # no externalId
		client = _client()
		with patch.object(client, "_request", side_effect=fake):
			client.update_item_serial("fb-item-1", "NEW")
		self.assertEqual(sent["body"]["serial"], "NEW")
		self.assertEqual(sent["body"]["externalId"], "NEW")

	def test_update_item_serial_keeps_custom_externalid(self):
		# If the externalId is NOT the serial (a custom key), don't clobber it —
		# only the serial moves.
		fake, sent = self._capture({"serial": "OLD", "externalId": "CUSTOM-KEY", "itemNumber": "1"})
		client = _client()
		with patch.object(client, "_request", side_effect=fake):
			client.update_item_serial("fb-item-1", "NEW")
		self.assertEqual(sent["body"]["serial"], "NEW")
		self.assertEqual(sent["body"]["externalId"], "CUSTOM-KEY")

	def test_update_item_serial_strips_and_caps_note(self):
		fake, sent = self._capture({"serial": "OLD", "itemNumber": "1"})
		client = _client()
		with patch.object(client, "_request", side_effect=fake):
			client.update_item_serial("fb-item-1", "  NEW  ", note="x" * 1500)
		body = sent["body"]
		self.assertEqual(body["serial"], "NEW")          # serial trimmed
		self.assertEqual(len(body["note"]), 1000)         # note capped at FastBound's limit

	def test_update_item_serial_omits_note_when_absent(self):
		fake, sent = self._capture({"serial": "OLD", "itemNumber": "1"})
		client = _client()
		with patch.object(client, "_request", side_effect=fake):
			client.update_item_serial("fb-item-1", "NEW")
		self.assertNotIn("note", sent["body"])           # no empty note key on the PUT


class FastBoundDownload4473Pdf(FrappeTestCase):
	"""GET /Form4473s/Download/{id} answers raw PDF bytes, and 400 "Invalid Audit
	User" without X-AuditUser (verified against the live account 2026-09-24) —
	so the bytes must not go through _request's JSON parse, and the audit header
	must ride along."""

	def test_returns_raw_bytes_with_audit_header(self):
		client = _client()
		client.session.request.return_value = MagicMock(status_code=200, content=b"%PDF-1.7 x")
		self.assertEqual(client.download_4473_pdf("f-1"), b"%PDF-1.7 x")
		args, kwargs = client.session.request.call_args
		self.assertEqual(args[0], "GET")
		self.assertTrue(args[1].endswith("/TEST/api/Form4473s/Download/f-1"), args[1])
		self.assertEqual(kwargs["headers"]["X-AuditUser"], "u")
		self.assertEqual(kwargs["headers"]["Accept"], "application/octet-stream")

	def test_error_status_raises_with_status_code(self):
		from ffl_integrations.fastbound.client import FastBoundError
		client = _client()
		client.session.request.return_value = MagicMock(status_code=404, text="", content=b"")
		with self.assertRaises(FastBoundError) as ctx:
			client.download_4473_pdf("f-1")
		self.assertEqual(ctx.exception.status_code, 404)
