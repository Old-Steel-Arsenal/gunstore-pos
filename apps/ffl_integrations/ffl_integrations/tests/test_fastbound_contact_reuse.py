"""Contact-reuse on the FastBound acquisition push.

Regression: individual-source acquisitions always created a fresh inline contact,
so a second gun from the same seller (e.g. several consignment guns in one batch)
400'd `This contact already exists`. The push must reuse an existing FastBound
contact — by our stable externalId, falling back to a name match — and only
create when the seller is genuinely new.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.fastbound.client import (
	FastBoundClient,
	FastBoundConfig,
	FastBoundError,
)


class FBContactReuse(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		s = frappe.get_single("FastBound Settings")
		if not s.account_num:
			s.account_num = "TEST-ACC"
			s.flags.ignore_permissions = True
			s.save()

	# ---------------------------------------------------------------- helpers
	def _client(self) -> FastBoundClient:
		"""A client whose HTTP layer we patch — no real settings/network."""
		cfg = FastBoundConfig(
			account_num="T", api_key="k", base_url="https://fb.test",
			api_version="1.0", audit_user="a@test", timeout=5,
		)
		return FastBoundClient(cfg, session=object())

	def _individual_acquisition(self, suffix: str, customer: str = "Haoquan Wang") -> str:
		from ffl_core.acquisition import record_firearm_acquisition
		if not frappe.db.exists("Customer Group", "FFL Test CG"):
			frappe.get_doc({
				"doctype": "Customer Group", "customer_group_name": "FFL Test CG",
				"parent_customer_group": "All Customer Groups", "is_group": 0,
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Territory", "FFL Test Territory"):
			frappe.get_doc({
				"doctype": "Territory", "territory_name": "FFL Test Territory",
				"parent_territory": "All Territories", "is_group": 0,
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Customer", customer):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": customer,
				"customer_type": "Individual",
				"customer_group": "FFL Test CG", "territory": "FFL Test Territory",
			}).insert(ignore_permissions=True)
		code = f"IND-ITEM-{suffix}"
		if not frappe.db.exists("Item", code):
			frappe.get_doc({
				"doctype": "Item", "item_code": code, "item_name": code,
				"item_group": "Rifle", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1,
			}).insert(ignore_permissions=True)
		return record_firearm_acquisition(
			item_code=code, serial=f"IND-SN-{suffix}",
			source_type="Individual", source_name=customer,
			source_id_type="Driver License", source_id_number="DL-123",
			source_customer=customer,
			cost=100, company=self.__class__.company,
		)

	# ---------------------------------------------------------------- tests
	def test_individual_contact_carries_stable_external_id(self):
		"""An individual seller's contact gets a stable externalId derived from the
		linked Customer, so a later push for the same seller finds it instead of
		creating a duplicate."""
		a = self._individual_acquisition(self._testMethodName)
		doc = frappe.get_doc("FFL Acquisition", a)
		contact = FastBoundClient._acquisition_payload(doc)["contact"]
		self.assertEqual(contact["externalId"], f"customer-{doc.source_customer}")

	def test_find_contact_by_name_returns_single_match(self):
		"""FastBound ignores the firstName/lastName query filter (as it does
		fflNumber), so we paginate and match client-side, returning the one hit."""
		client = self._client()
		pages = [{"contacts": [
			{"id": "c1", "firstName": "Haoquan", "lastName": "Wang"},
			{"id": "c2", "firstName": "Jane", "lastName": "Doe"},
		]}]
		with patch.object(client, "_request", side_effect=pages):
			found = client.find_contact_by_name("Haoquan", "Wang")
		self.assertEqual(found["id"], "c1")

	def test_find_contact_by_name_ambiguous_raises(self):
		"""Two contacts with the same name (case-insensitive) is ambiguous — refuse
		to guess which seller rather than link the bound book to the wrong person."""
		client = self._client()
		pages = [{"contacts": [
			{"id": "c1", "firstName": "Haoquan", "lastName": "Wang"},
			{"id": "c2", "firstName": "haoquan", "lastName": "WANG"},
		]}]
		with patch.object(client, "_request", side_effect=pages):
			with self.assertRaises(FastBoundError):
				client.find_contact_by_name("Haoquan", "Wang")

	def test_individual_push_references_existing_contact(self):
		"""When the seller already has a FastBound contact (here found by name —
		the externalId is absent because the contact predates this fix), the push
		references it by ContactId and sends NO inline contact, so FastBound can't
		400 'This contact already exists'."""
		a = self._individual_acquisition(self._testMethodName)
		doc = frappe.get_doc("FFL Acquisition", a)
		client = self._client()
		with patch.object(client, "find_contact_by_external_id", return_value=None), \
				patch.object(client, "find_contact_by_name", return_value={"id": "existing-contact"}), \
				patch.object(client, "_request", return_value={"id": "fb-acq-1", "items": []}) as req:
			result = client.post_acquisition(doc)
		self.assertEqual(result["acquisitionId"], "fb-acq-1")
		self.assertEqual(req.call_count, 1, "only the CreateAndCommit POST should hit the wire")
		body = req.call_args.kwargs["json_body"]
		self.assertEqual(body.get("ContactId"), "existing-contact")
		self.assertNotIn("contact", body)

	def test_individual_push_recovers_from_contact_exists_race(self):
		"""Concurrent pushes for one new seller: our resolve finds nothing, so we
		try an inline create — but a sibling push won the race and FastBound 400s
		'This contact already exists'. The push must re-resolve the now-existing
		contact and retry by ContactId, not fail and get stuck."""
		a = self._individual_acquisition(self._testMethodName)
		doc = frappe.get_doc("FFL Acquisition", a)
		client = self._client()
		race = FastBoundError(
			'POST Acquisitions/CreateAndCommit → 400: '
			'{"errors":[{"message":"This contact already exists."}]}',
			status_code=400,
		)
		with patch.object(client, "find_contact_by_external_id", return_value=None), \
				patch.object(client, "find_contact_by_name", side_effect=[None, {"id": "won-contact"}]), \
				patch.object(client, "_request",
					side_effect=[race, {"id": "fb-acq-2", "items": []}]) as req:
			result = client.post_acquisition(doc)
		self.assertEqual(result["acquisitionId"], "fb-acq-2")
		self.assertEqual(req.call_count, 2, "should retry the commit once after re-resolving")
		retry_body = req.call_args_list[1].kwargs["json_body"]
		self.assertEqual(retry_body.get("ContactId"), "won-contact")
		self.assertNotIn("contact", retry_body)


if __name__ == "__main__":
	unittest.main()
