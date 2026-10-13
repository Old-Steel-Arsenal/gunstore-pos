"""Tests for the ShipStation shipment builder + the manual Ship push.

Builder tests use real ATF FFL Record / Address fixtures plus a fake Sales
Invoice doc (frappe.get_doc routed), so no ERPNext invoice scaffolding is
needed. push_shipment tests patch the client — no network.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from ffl_integrations.shipstation import api as ss_api
from ffl_integrations.shipstation.client import (
	ShipStationClient,
	ShipStationConfig,
	ShipStationError,
)

# Module-unique FFL numbers (fresh-DB collision rule: unique per test module).
FFL_VALID = "9-99-77777-SS-PUSH-1"
FFL_NO_DBA = "9-99-77777-SS-PUSH-2"
FFL_EXPIRED = "9-99-77777-SS-PUSH-3"

_REAL_GET_DOC = frappe.get_doc


def _cfg(**over) -> ShipStationConfig:
	base = dict(api_key="TEST_key", base_url="https://api.shipstation.com/v2",
		store_id="", ship_from_warehouse_id="", default_parcel_weight_oz=16.0,
		timeout=5, sandbox_mode=True)
	base.update(over)
	return ShipStationConfig(**base)


class _FakeSI:
	"""Attribute-bag Sales Invoice stand-in. NOT frappe._dict — `si.items` on a
	dict resolves to the builtin dict.items method instead of the child rows."""

	def __init__(self, **kw):
		self.__dict__.update(kw)

	def get(self, key, default=None):
		return getattr(self, key, default)


def _fake_si(**over) -> _FakeSI:
	base = dict(
		doctype="Sales Invoice",
		name="SI-SS-TEST-0001",
		docstatus=1,
		customer="CUST-0001",
		customer_name="John Buyer",
		contact_email="john@example.com",
		ffl_buyer_type="Individual",
		outstanding_amount=0,
		ffl_destination_ffl=FFL_VALID,
		shipping_address_name=None,
		shipstation_order_id=None,
		shipstation_status="Not Requested",
		items=[frappe._dict(item_code="SS-TEST-GLOCK-19", item_name="Glock 19 Gen5",
			qty=1, serial_no="SN-ABC-1")],
	)
	base.update(over)
	return _FakeSI(**base)


def _route_get_doc(fake_si):
	def router(doctype, *args, **kwargs):
		if doctype == "Sales Invoice":
			return fake_si
		return _REAL_GET_DOC(doctype, *args, **kwargs)
	return router


def _ensure_ffl(number: str, *, dba: str = "", expired: bool = False) -> None:
	if frappe.db.exists("ATF FFL Record", number):
		return
	frappe.get_doc({
		"doctype": "ATF FFL Record",
		"ffl_number": number,
		"licensee_name": "PALMETTO ARMORY LLC",
		"licensee_dba": dba,
		"street": "123 Range Rd",
		"street2": "Suite 7",
		"city": "Austin",
		"state": "TX",
		"zip": "78701",
		"phone": "512-555-0100",
		"expiry_date": add_days(nowdate(), -10 if expired else 365),
	}).insert(ignore_permissions=True)


class ShipStationFixtures(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_ffl(FFL_VALID, dba="Palmetto Guns")
		_ensure_ffl(FFL_NO_DBA)
		_ensure_ffl(FFL_EXPIRED, expired=True)


# ---------------------------------------------------------------------------
# _to_ounces — pure unit conversion
# ---------------------------------------------------------------------------

class TestToOunces(FrappeTestCase):
	def test_pounds(self):
		self.assertAlmostEqual(ss_api._to_ounces(2, "lb"), 32.0)
		self.assertAlmostEqual(ss_api._to_ounces(1, "Lbs"), 16.0)
		self.assertAlmostEqual(ss_api._to_ounces(1, "Pound"), 16.0)

	def test_metric(self):
		self.assertAlmostEqual(ss_api._to_ounces(1, "kg"), 35.274, places=3)
		self.assertAlmostEqual(ss_api._to_ounces(500, "Gram"), 17.637, places=3)

	def test_ounces_passthrough(self):
		self.assertAlmostEqual(ss_api._to_ounces(12, "oz"), 12.0)
		self.assertAlmostEqual(ss_api._to_ounces(12, "Ounce"), 12.0)

	def test_unknown_or_blank_uom_treated_as_ounces(self):
		self.assertAlmostEqual(ss_api._to_ounces(9, "Unit"), 9.0)
		self.assertAlmostEqual(ss_api._to_ounces(9, ""), 9.0)
		self.assertAlmostEqual(ss_api._to_ounces(9, None), 9.0)


# ---------------------------------------------------------------------------
# build_ship_to
# ---------------------------------------------------------------------------

class TestBuildShipTo(ShipStationFixtures):
	def _envelope(self, si):
		with patch("frappe.get_doc", side_effect=_route_get_doc(si)):
			return ss_api.build_ship_to(si.name)

	def test_recipient_is_customer_and_company_is_dba(self):
		env = self._envelope(_fake_si())
		ship_to = env["ship_to"]
		self.assertEqual(ship_to["name"], "John Buyer")
		self.assertEqual(ship_to["company_name"], "Palmetto Guns")
		self.assertEqual(ship_to["address_line1"], "123 Range Rd")
		self.assertEqual(ship_to["city_locality"], "Austin")
		self.assertEqual(ship_to["state_province"], "TX")
		self.assertEqual(ship_to["postal_code"], "78701")
		self.assertEqual(ship_to["country_code"], "US")
		self.assertEqual(ship_to["address_residential_indicator"], "no")

	def test_company_falls_back_to_licensee_name_without_dba(self):
		env = self._envelope(_fake_si(ffl_destination_ffl=FFL_NO_DBA))
		self.assertEqual(env["ship_to"]["company_name"], "PALMETTO ARMORY LLC")

	def test_ffl_number_rides_envelope_not_ship_to(self):
		env = self._envelope(_fake_si())
		self.assertEqual(env["ffl_number"], FFL_VALID)
		self.assertNotIn("ffl_number", env["ship_to"])

	def test_street2_rides_as_address_line2(self):
		env = self._envelope(_fake_si())
		self.assertEqual(env["ship_to"]["address_line2"], "Suite 7")

	def test_non_ffl_order_falls_back_to_shipping_address(self):
		addr = frappe.get_doc({
			"doctype": "Address",
			"address_title": "John Buyer Home",
			"address_type": "Shipping",
			"address_line1": "9 Home St",
			"city": "Dallas",
			"state": "TX",
			"pincode": "75201",
			"country": "United States",
		}).insert(ignore_permissions=True)
		si = _fake_si(ffl_destination_ffl=None, shipping_address_name=addr.name)
		env = self._envelope(si)
		ship_to = env["ship_to"]
		self.assertEqual(ship_to["name"], "John Buyer")
		self.assertEqual(ship_to["address_line1"], "9 Home St")
		self.assertEqual(ship_to["city_locality"], "Dallas")
		self.assertEqual(ship_to["postal_code"], "75201")
		self.assertIsNone(env["ffl_number"])

	def test_serials_suffixed_into_item_names_and_default_weight(self):
		with patch("frappe.db.get_single_value", return_value=24):
			env = self._envelope(_fake_si())
		item = env["items"][0]
		self.assertIn("SN: SN-ABC-1", item["name"])
		self.assertEqual(item["weight"], {"value": 24.0, "unit": "ounce"})

	def test_item_prices_and_order_money_ride_the_envelope(self):
		"""ShipStation renders Unit Cost / Product / Tax / Total Paid from these;
		sending none of them left every order at $0.00.
		Freight lives inside total_taxes_and_charges as an Actual row, so the
		tax term is the remainder once ffl_shipping_amount comes out."""
		si = _fake_si(
			items=[frappe._dict(item_code="SS-TEST-GLOCK-19", item_name="Glock 19 Gen5",
				qty=1, serial_no="SN-ABC-1", rate=550)],
			grand_total=620.38, outstanding_amount=0,
			total_taxes_and_charges=70.38, ffl_shipping_amount=25)
		env = self._envelope(si)
		self.assertEqual(env["items"][0]["unit_price"], 550.0)
		self.assertEqual(env["currency"], "usd")
		self.assertEqual(env["amounts"], {
			"amount_paid": 620.38, "tax_paid": 45.38, "shipping_paid": 25.0})

	def test_unpaid_order_sends_no_money_at_all(self):
		"""A dealer order may ship on account. $0 paid next to "$45 tax paid"
		reads like a bug; an absent block reads like the truth it is."""
		si = _fake_si(grand_total=100, outstanding_amount=100,
			total_taxes_and_charges=8.25, ffl_shipping_amount=25)
		self.assertEqual(self._envelope(si)["amounts"], {})

	def test_mixed_order_drops_the_money_with_the_dropshipped_line(self):
		"""The RSR line never enters `items`, but the invoice totals cover
		it — stamping them on a holster-only parcel would read "$920 paid"."""
		si = _fake_si(items=[
			frappe._dict(item_code="SS-TEST-CASE-1", item_name="Holster",
				qty=1, serial_no=None, rate=20),
			frappe._dict(item_code="SS-TEST-GLOCK-19", item_name="Glock 19 Gen5",
				qty=1, serial_no="SN-RSR-1", rate=900, delivered_by_supplier=1),
		], grand_total=995.85, outstanding_amount=0, total_taxes_and_charges=75.85)
		env = self._envelope(si)
		self.assertEqual([i["sku"] for i in env["items"]], ["SS-TEST-CASE-1"])
		self.assertEqual(env["amounts"], {})

	def test_return_without_a_freight_row_never_reports_negative_tax(self):
		si = _fake_si(grand_total=100, outstanding_amount=0,
			total_taxes_and_charges=0, ffl_shipping_amount=25)
		self.assertEqual(self._envelope(si)["amounts"]["tax_paid"], 0.0)

	def test_line_without_serials_keeps_plain_name(self):
		si = _fake_si(items=[frappe._dict(item_code="SS-TEST-CASE-1",
			item_name="Pistol Case", qty=2, serial_no=None)])
		env = self._envelope(si)
		self.assertEqual(env["items"][0]["name"], "Pistol Case")
		self.assertEqual(env["items"][0]["quantity"], 2)

	def test_fractional_qty_floors_to_one_not_zero(self):
		"""cint(0.5) is 0 — a zero-quantity item would be rejected by the API
		and zero out its weight term; a sub-unit line still ships one parcel."""
		si = _fake_si(items=[frappe._dict(item_code="SS-TEST-CASE-1",
			item_name="Bulk Ammo", qty=0.5, serial_no=None)])
		env = self._envelope(si)
		self.assertEqual(env["items"][0]["quantity"], 1)


# ---------------------------------------------------------------------------
# build_shipment — the exact V2 POST body
# ---------------------------------------------------------------------------

class TestBuildShipment(ShipStationFixtures):
	def _shipment(self, si, cfg):
		with patch("frappe.get_doc", side_effect=_route_get_doc(si)):
			return ss_api.build_shipment(si.name, config=cfg)

	def test_body_shape(self):
		si = _fake_si(items=[
			frappe._dict(item_code="SS-TEST-GLOCK-19", item_name="Glock 19 Gen5",
				qty=1, serial_no="SN-ABC-1"),
			frappe._dict(item_code="SS-TEST-CASE-1", item_name="Pistol Case",
				qty=2, serial_no=None),
		])
		with patch("frappe.db.get_single_value", return_value=0):
			body = self._shipment(si, _cfg(default_parcel_weight_oz=10,
				ship_from_warehouse_id="se-wh-1"))
		self.assertEqual(body["external_shipment_id"], si.name)
		self.assertEqual(body["external_order_id"], si.name)
		self.assertTrue(body["create_sales_order"])
		self.assertEqual(body["ship_to"]["name"], "John Buyer")
		self.assertNotIn("ffl_number", body["ship_to"])
		self.assertNotIn("carrier_id", body)
		self.assertNotIn("service_code", body)
		self.assertNotIn("store_id", body)
		self.assertEqual(body["warehouse_id"], "se-wh-1")
		self.assertEqual([i["sku"] for i in body["items"]], ["SS-TEST-GLOCK-19", "SS-TEST-CASE-1"])
		for item in body["items"]:
			self.assertNotIn("weight", item)
			# The live V2 API rejects "1.0": quantity must be a JSON integer.
			self.assertIsInstance(item["quantity"], int)
		self.assertEqual([i["quantity"] for i in body["items"]], [1, 2])
		# 1 gun × 10oz default + 2 cases × 10oz default = 30oz total parcel.
		self.assertEqual(body["packages"], [{"weight": {"value": 30.0, "unit": "ounce"}}])

	def test_money_rides_the_body_and_zeros_are_omitted(self):
		si = _fake_si(
			items=[frappe._dict(item_code="SS-TEST-GLOCK-19", item_name="Glock 19 Gen5",
				qty=1, serial_no="SN-ABC-1", rate=550)],
			grand_total=595.38, outstanding_amount=0, total_taxes_and_charges=45.38,
			ffl_shipping_amount=0)
		body = self._shipment(si, _cfg(ship_from_warehouse_id="se-wh-1"))
		self.assertEqual(body["items"][0]["unit_price"], 550.0)
		self.assertEqual(body["amount_paid"], {"currency": "usd", "amount": 595.38})
		self.assertEqual(body["tax_paid"], {"currency": "usd", "amount": 45.38})
		# No freight on this order — an absent field reads the same as a zero.
		self.assertNotIn("shipping_paid", body)

	def test_store_and_warehouse_included_when_configured(self):
		body = self._shipment(_fake_si(),
			_cfg(store_id="1582173", ship_from_warehouse_id="se-wh-9"))
		self.assertEqual(body["store_id"], "1582173")
		self.assertEqual(body["warehouse_id"], "se-wh-9")

	def test_internal_notes_carry_all_serials(self):
		"""Staff-facing: every firearm serial rides in internal_notes (the
		order-sidebar notes in ShipStation) in addition to the item names."""
		si = _fake_si(items=[
			frappe._dict(item_code="SS-TEST-GLOCK-19", item_name="Glock 19 Gen5",
				qty=1, serial_no="SN-ABC-1"),
			frappe._dict(item_code="SS-TEST-GLOCK-19", item_name="Glock 19 Gen5",
				qty=1, serial_no="SN-ABC-2"),
			frappe._dict(item_code="SS-TEST-CASE-1", item_name="Pistol Case",
				qty=1, serial_no=None),
		])
		body = self._shipment(si, _cfg(ship_from_warehouse_id="se-wh-1"))
		self.assertIn("SN-ABC-1", body["internal_notes"])
		self.assertIn("SN-ABC-2", body["internal_notes"])

	def test_no_serials_no_internal_notes(self):
		si = _fake_si(items=[frappe._dict(item_code="SS-TEST-CASE-1",
			item_name="Pistol Case", qty=1, serial_no=None)])
		body = self._shipment(si, _cfg(ship_from_warehouse_id="se-wh-1"))
		self.assertNotIn("internal_notes", body)

	def test_missing_warehouse_raises_config_error(self):
		"""The live V2 API 400s with 'ship_from is required when warehouse_id
		is not present' — fail fast with an actionable settings message."""
		from ffl_integrations.shipstation.client import ShipStationConfigError
		with self.assertRaises(ShipStationConfigError) as cm:
			self._shipment(_fake_si(), _cfg(ship_from_warehouse_id=""))
		self.assertIn("Ship-From Warehouse", str(cm.exception))


# ---------------------------------------------------------------------------
# push_shipment — gates, persistence, idempotency
# ---------------------------------------------------------------------------

class TestPushShipment(ShipStationFixtures):
	def _push(self, si, *, enabled=True, client=None):
		"""Run push_shipment with the SI faked and the client mocked. Returns
		(result, mock_client, db_set_value_mock)."""
		client = client or MagicMock()
		if not isinstance(client.create_shipment.return_value, dict):
			client.create_shipment.return_value = {"shipment_id": "se-42"}
		if isinstance(client.find_shipment_by_external_id.return_value, MagicMock):
			client.find_shipment_by_external_id.return_value = None
		real_get_value = frappe.db.get_value

		def route_get_value(doctype, *args, **kwargs):
			if doctype == "Sales Invoice":
				# The under-lock re-read of the push guard fields.
				return frappe._dict(
					shipstation_order_id=si.shipstation_order_id,
					shipstation_status=si.shipstation_status)
			return real_get_value(doctype, *args, **kwargs)

		with patch("frappe.get_doc", side_effect=_route_get_doc(si)), \
			patch.object(ss_api, "_enabled", return_value=enabled), \
			patch.object(ss_api, "build_shipment", return_value={"ship_to": {}}), \
			patch.object(ShipStationClient, "from_settings", return_value=client), \
			patch("frappe.db.get_value", side_effect=route_get_value), \
			patch("frappe.db.set_value") as set_value:
			result = ss_api.push_shipment(si.name)
		return result, client, set_value

	def test_disabled_throws(self):
		with self.assertRaises(frappe.ValidationError):
			self._push(_fake_si(), enabled=False)

	def test_draft_invoice_throws(self):
		with self.assertRaises(frappe.ValidationError):
			self._push(_fake_si(docstatus=0))

	def test_already_pushed_by_order_id_is_noop(self):
		si = _fake_si(shipstation_order_id="se-99")
		result, client, set_value = self._push(si)
		self.assertTrue(result["already"])
		self.assertEqual(result["shipstation_order_id"], "se-99")
		client.create_shipment.assert_not_called()
		set_value.assert_not_called()

	def test_already_pushed_by_status_is_noop(self):
		si = _fake_si(shipstation_status="Pending")
		result, client, set_value = self._push(si)
		self.assertTrue(result["already"])
		client.create_shipment.assert_not_called()

	def test_unpaid_individual_throws(self):
		si = _fake_si(outstanding_amount=500)
		with self.assertRaises(frappe.ValidationError):
			self._push(si)

	def test_unpaid_business_allowed(self):
		si = _fake_si(outstanding_amount=500, ffl_buyer_type="Business")
		result, client, _ = self._push(si)
		self.assertTrue(result["ok"])
		client.create_shipment.assert_called_once()

	def test_expired_ffl_throws(self):
		si = _fake_si(ffl_destination_ffl=FFL_EXPIRED)
		with self.assertRaises(frappe.ValidationError):
			self._push(si)

	def test_success_persists_order_id_and_pending_status(self):
		result, _, set_value = self._push(_fake_si())
		self.assertEqual(result["shipstation_order_id"], "se-42")
		set_value.assert_called_once_with(
			"Sales Invoice", "SI-SS-TEST-0001",
			{"shipstation_order_id": "se-42", "shipstation_status": "Pending"},
			update_modified=False)

	def test_push_adopts_existing_external_shipment(self):
		"""Lost-response healing: when ShipStation already holds a live shipment
		for this invoice (external_shipment_id match), adopt its id instead of
		double-creating."""
		client = MagicMock()
		client.find_shipment_by_external_id.return_value = {
			"shipment_id": "se-earlier", "shipment_status": "pending"}
		result, client, set_value = self._push(_fake_si(), client=client)
		self.assertTrue(result["already"])
		self.assertEqual(result["shipstation_order_id"], "se-earlier")
		client.create_shipment.assert_not_called()
		set_value.assert_called_once_with(
			"Sales Invoice", "SI-SS-TEST-0001",
			{"shipstation_order_id": "se-earlier", "shipstation_status": "Pending"},
			update_modified=False)

	def test_push_prechecks_external_id_before_creating(self):
		result, client, _ = self._push(_fake_si())
		client.find_shipment_by_external_id.assert_called_once_with("SI-SS-TEST-0001")
		client.create_shipment.assert_called_once()

	def test_api_error_throws_and_persists_nothing(self):
		client = MagicMock()
		client.create_shipment.side_effect = ShipStationError("boom", status_code=400)
		client.find_shipment_by_external_id.return_value = None
		si = _fake_si()
		with patch("frappe.get_doc", side_effect=_route_get_doc(si)), \
			patch.object(ss_api, "_enabled", return_value=True), \
			patch.object(ss_api, "build_shipment", return_value={"ship_to": {}}), \
			patch.object(ShipStationClient, "from_settings", return_value=client), \
			patch("frappe.db.set_value") as set_value:
			with self.assertRaises(frappe.ValidationError):
				ss_api.push_shipment(si.name)
			set_value.assert_not_called()


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------

class TestTestConnection(FrappeTestCase):
	def test_config_error_is_friendly(self):
		from ffl_integrations.shipstation.client import ShipStationConfigError
		with patch.object(ShipStationClient, "from_settings",
				side_effect=ShipStationConfigError("API key missing")):
			with self.assertRaises(frappe.ValidationError) as cm:
				ss_api.test_connection()
		self.assertIn("not configured", str(cm.exception))

	def test_ok_returns_carrier_count_and_warehouses(self):
		client = MagicMock()
		client.probe_connection.return_value = {"carriers": [{"c": 1}, {"c": 2}]}
		client.list_warehouses.return_value = [
			{"warehouse_id": "se-1", "name": "Main", "is_default": True}]
		client.config = _cfg(sandbox_mode=True)
		with patch.object(ShipStationClient, "from_settings", return_value=client):
			result = ss_api.test_connection()
		self.assertTrue(result["ok"])
		self.assertEqual(result["carrier_count"], 2)
		self.assertTrue(result["sandbox"])
		self.assertEqual(result["warehouses"][0]["warehouse_id"], "se-1")

	def test_warehouses_skipped_when_already_configured(self):
		client = MagicMock()
		client.probe_connection.return_value = {"carriers": [{"c": 1}]}
		client.config = _cfg(ship_from_warehouse_id="se-wh-1")
		with patch.object(ShipStationClient, "from_settings", return_value=client):
			result = ss_api.test_connection()
		self.assertTrue(result["ok"])
		client.list_warehouses.assert_not_called()
		self.assertEqual(result["warehouses"], [])

	def test_warehouses_failure_does_not_mask_valid_probe(self):
		"""The probe already proved the key — a warehouses-endpoint hiccup must
		not report a valid connection as failed."""
		client = MagicMock()
		client.probe_connection.return_value = {"carriers": [{"c": 1}]}
		client.list_warehouses.side_effect = ShipStationError("500", status_code=500)
		client.config = _cfg(ship_from_warehouse_id="")
		with patch.object(ShipStationClient, "from_settings", return_value=client):
			result = ss_api.test_connection()
		self.assertTrue(result["ok"])
		self.assertEqual(result["warehouses"], [])
