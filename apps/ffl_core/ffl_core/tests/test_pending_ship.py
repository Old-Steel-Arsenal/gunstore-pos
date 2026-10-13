"""Pending Order queue × ShipStation Ship button.

The queue (list_pending_dispositions) must keep a manual order visible until
it is disposed AND paid AND pushed to ShipStation (when the integration is
enabled) — previously a disposed+paid order vanished immediately, which would
make the new independent Ship button unreachable. Also covers the one-time
legacy stamp patch.

ShipStation enablement is ALWAYS patched (both ways) — never read from the
persistent dev DB, where manual smoke testing may have flipped the toggle.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api import manual_order
from ffl_core.tests.test_build_order import DEST_FFL, BuildOrderMixin


def _row(rows: list[dict], si_name: str) -> dict | None:
	return next((r for r in rows if r.get("sales_invoice") == si_name), None)


class PendingShipFixtures(BuildOrderMixin, FrappeTestCase):
	"""BuildOrderMixin (not BuildOrderFixtures) on purpose: the fixture CLASS
	carries six commit-heavy test methods that unittest would re-run for every
	subclass here — the mixin brings only the helpers."""
	def _rows(self, enabled: bool) -> list[dict]:
		with patch.object(manual_order, "_shipstation_enabled", return_value=enabled):
			return manual_order.list_pending_dispositions()

	def _paid(self, si_name: str) -> None:
		frappe.db.set_value(
			"Sales Invoice", si_name, "outstanding_amount", 0, update_modified=False)


class TestPendingShipRetention(PendingShipFixtures):
	def test_disposed_paid_row_retained_until_pushed(self):
		si = self._ship(self._stocked_serial("SS-PEND-1"), update_stock=1)
		self._paid(si.name)

		row = _row(self._rows(enabled=True), si.name)
		self.assertIsNotNone(row, "disposed+paid order must stay until pushed")
		self.assertTrue(row["disposed"])
		# The card's greyed fallback needs the disposed serials exposed once
		# pending empties — pin the producer side of that contract here.
		self.assertEqual([s["serial"] for s in row["disposed_serials"]], ["SS-PEND-1"])
		self.assertFalse(row["needs_disposition"])
		self.assertFalse(row["ship_pushed"])
		self.assertTrue(row["shipstation_enabled"])

		frappe.db.set_value("Sales Invoice", si.name,
			"shipstation_order_id", "se-123", update_modified=False)
		self.assertIsNone(_row(self._rows(enabled=True), si.name),
			"pushed+disposed+paid order must leave the queue")

	def test_status_stamp_alone_drops_row(self):
		si = self._ship(self._stocked_serial("SS-PEND-2"), update_stock=1)
		self._paid(si.name)
		frappe.db.set_value("Sales Invoice", si.name,
			"shipstation_status", "Shipped", update_modified=False)
		self.assertIsNone(_row(self._rows(enabled=True), si.name),
			"a legacy 'Shipped' stamp (no order id) must satisfy ship_pushed")

	def test_shipstation_disabled_preserves_today_semantics(self):
		si = self._ship(self._stocked_serial("SS-PEND-3"), update_stock=1)
		self._paid(si.name)
		self.assertIsNone(_row(self._rows(enabled=False), si.name),
			"with ShipStation off, disposed+paid drops immediately (today's rule)")

	def test_null_status_counts_as_unpushed(self):
		si = self._ship(self._stocked_serial("SS-PEND-4"), update_stock=1)
		self._paid(si.name)
		frappe.db.set_value("Sales Invoice", si.name,
			"shipstation_status", None, update_modified=False)
		row = _row(self._rows(enabled=True), si.name)
		self.assertIsNotNone(row, "NULL status (pre-fixture SI) must count as unpushed")
		self.assertFalse(row["ship_pushed"])

	def test_row_carries_ship_fields(self):
		serial = self._stocked_serial("SS-PEND-5")
		si = self._ship(serial, update_stock=0)  # deferred → not disposed
		# Pin the shared fixture FFL's address HERE: an inherited BuildOrder
		# test rewrites it mid-class and commit-heavy siblings can make that
		# stick, so asserting against setUpClass values is order-dependent.
		frappe.db.set_value("ATF FFL Record", DEST_FFL, {
			"street": "100 Test Pkwy", "street2": "", "city": "Springfield",
			"state": "TX", "zip": "75024",
		}, update_modified=False)
		row = _row(self._rows(enabled=True), si.name)
		self.assertIsNotNone(row)
		self.assertFalse(row["disposed"])
		self.assertTrue(row["needs_disposition"])
		# DEST_FFL fixture has no DBA → company falls back to licensee_name.
		self.assertEqual(row["ship_to_preview"]["company"],
			frappe.db.get_value("ATF FFL Record", DEST_FFL, "licensee_name"))
		self.assertEqual(row["ship_to_preview"]["line1"], "100 Test Pkwy")
		self.assertEqual(row["ship_to_preview"]["state"], "TX")
		gun_items = [i for i in row["ship_items"] if serial in (i.get("serials") or [])]
		self.assertTrue(gun_items, "ship_items must carry ALL serials for the Ship dialog")
		self.assertEqual(row["shipstation_status"], "Not Requested")


class TestMarkShippedManually(PendingShipFixtures):
	def test_clears_disposed_row(self):
		"""The stuck-row escape hatch: a disposed order that can't go through
		ShipStation (FFL expired after dispose, misconfigured integration,
		label bought elsewhere) must be clearable from the queue."""
		si = self._ship(self._stocked_serial("SS-MARK-1"), update_stock=1)
		self._paid(si.name)
		out = manual_order.mark_shipped_manually(si.name)
		self.assertTrue(out["ok"])
		self.assertEqual(frappe.db.get_value(
			"Sales Invoice", si.name, "shipstation_status"), "Shipped")
		self.assertFalse(frappe.db.get_value(
			"Sales Invoice", si.name, "shipstation_order_id"))
		self.assertIsNone(_row(self._rows(enabled=True), si.name))

	def test_marks_an_undisposed_order_and_keeps_it_queued(self):
		"""User decision 2026-09-11: an undisposed firearm no longer refuses —
		Mark Shipped clears the SHIPPING half, exactly like the ShipStation
		push it substitutes for (which never required a dispose). What must
		NOT happen is the gun going quiet: no disposition is booked, the row
		stays on the queue with needs_disposition, and the caller is warned."""
		si = self._ship(self._stocked_serial("SS-MARK-2"), update_stock=0)
		self._paid(si.name)
		out = manual_order.mark_shipped_manually(si.name)
		self.assertTrue(out["ok"])
		self.assertEqual(out["undisposed_serials"], ["SS-MARK-2"])
		self.assertEqual(frappe.db.get_value(
			"Sales Invoice", si.name, "shipstation_status"), "Shipped")
		# No disposition was booked by this path — the bound book is untouched.
		self.assertFalse(frappe.get_all("FFL Disposition",
			filters={"sales_invoice": si.name, "docstatus": 1}))
		# ...and the row is still here, asking for the Dispose.
		row = _row(self._rows(enabled=True), si.name)
		self.assertIsNotNone(row, "a marked-shipped undisposed order must stay queued")
		self.assertTrue(row["needs_disposition"])
		self.assertEqual([p["serial"] for p in row["pending_serials"]], ["SS-MARK-2"])

	def test_the_stamp_does_not_move_the_undisposed_gun_stock(self):
		"""The load-bearing half of the relaxation: a regulated firearm's stock
		leaves through its disposition's Material Issue. If Mark Shipped also
		issued it, the later Dispose would double-book — so the serial must
		still be in stock after the stamp."""
		serial = self._stocked_serial("SS-MARK-4")
		si = self._ship(serial, update_stock=0)
		self._paid(si.name)
		manual_order.mark_shipped_manually(si.name)
		self.assertEqual(frappe.db.get_value("Serial No", serial, "status"), "Active")

	def test_still_refuses_firearm_lines_with_no_serial_state(self):
		"""The anomaly guard survives: regulated lines whose serials resolve to
		nothing at all (neither pending nor disposed) are a data fault, not an
		order waiting on a dispose, and must still fail closed."""
		si = self._ship(self._stocked_serial("SS-MARK-5"), update_stock=0)
		self._paid(si.name)
		with patch("ffl_core.api.manual_order.si_firearm_serial_state",
				return_value=([], set(), [])):
			with self.assertRaises(frappe.ValidationError) as cm:
				manual_order.mark_shipped_manually(si.name)
		self.assertIn("no serial/disposition state", str(cm.exception))

	def test_noop_when_already_pushed(self):
		si = self._ship(self._stocked_serial("SS-MARK-3"), update_stock=1)
		frappe.db.set_value("Sales Invoice", si.name,
			"shipstation_order_id", "se-555", update_modified=False)
		out = manual_order.mark_shipped_manually(si.name)
		self.assertTrue(out.get("already"))
		self.assertNotEqual(frappe.db.get_value(
			"Sales Invoice", si.name, "shipstation_status"), "Shipped")


class TestShipAddressPhone(PendingShipFixtures):
	def test_ffl_branch_carries_premise_phone(self):
		frappe.db.set_value("ATF FFL Record", DEST_FFL,
			"phone", "972-555-0111", update_modified=False)
		ship_to, _recipient = manual_order._resolve_ship_to({}, DEST_FFL)
		self.assertEqual(ship_to.get("phone"), "972-555-0111")

	def test_payload_branch_carries_recipient_phone(self):
		payload = {"ship_to": {"address_line1": "9 Home St", "city": "Dallas",
			"state": "TX", "pincode": "75201"}, "recipient_name": "John",
			"recipient_phone": "214-555-0000"}
		ship_to, _recipient = manual_order._resolve_ship_to(payload, None)
		self.assertEqual(ship_to.get("phone"), "214-555-0000")

	def test_ensure_ship_address_stores_phone(self):
		addr_name = manual_order._ensure_ship_address(
			"_Test BO Individual", "John Buyer",
			{"address_line1": "77 Phone Ln", "city": "Springfield", "state": "TX",
				"pincode": "75024", "phone": "469-555-0000"})
		self.assertTrue(addr_name)
		self.assertEqual(frappe.db.get_value("Address", addr_name, "phone"),
			"469-555-0000")


class TestFindSerialForOrder(PendingShipFixtures):
	"""Scan-to-add on the Create Order page: a serial number resolves straight
	to an order line, failing loud with the exact reason a gun can't sell."""

	def test_resolves_stocked_serial_case_insensitively(self):
		serial = self._stocked_serial("SS-SCAN-1")
		from ffl_core.firearm import available_serials_with_prices
		# Price oracle must mirror find_serial_for_order's OWN path: it reads the
		# price via _serial_ctx_for_item → available_serials_with_prices(
		# exclude_unavailable=False). The default now drops held/consignment-
		# warehouse guns — irrelevant to a price cross-check, and the
		# shared BuildOrderMixin fixture's cls.warehouse can itself be a
		# consignment warehouse on a warm DB, so pass False to compare like-for-
		# like against the raw on-hand list.
		expected_price = next(
			r["sell_price"] for r in available_serials_with_prices(
				["BO-GUN"], exclude_unavailable=False)["BO-GUN"]
			if r["serial"] == serial)
		out = manual_order.find_serial_for_order("  ss-scan-1  ")
		self.assertEqual(out["serial"], "SS-SCAN-1")
		self.assertEqual(out["item_code"], "BO-GUN")
		self.assertTrue(out["item_name"])
		self.assertTrue(out["is_firearm"])
		self.assertEqual(out["sell_price"], expected_price)

	def test_unknown_serial_throws(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			manual_order.find_serial_for_order("NO-SUCH-SERIAL-XYZ")
		self.assertIn("on file", str(cm.exception))

	def test_shipped_serial_not_in_stock(self):
		serial = self._stocked_serial("SS-SCAN-SOLD")
		self._ship(serial, update_stock=1)
		with self.assertRaises(frappe.ValidationError) as cm:
			manual_order.find_serial_for_order(serial)
		self.assertIn("not in stock", str(cm.exception))

	def test_serial_held_by_open_counter_order_throws(self):
		serial = self._stocked_serial("SS-SCAN-HELD")
		si = self._ship(serial, update_stock=0)  # deferred → still Active but held
		with self.assertRaises(frappe.ValidationError) as cm:
			manual_order.find_serial_for_order(serial)
		self.assertIn(si.name, str(cm.exception))

	def test_woo_reserved_serial_throws(self):
		serial = self._stocked_serial("SS-SCAN-WOO")
		frappe.db.set_value("Serial No", serial, "woo_reserved", 1,
			update_modified=False)
		with self.assertRaises(frappe.ValidationError) as cm:
			manual_order.find_serial_for_order(serial)
		self.assertIn("web order", str(cm.exception))

	def test_non_firearm_serial_throws(self):
		if not frappe.db.exists("Item", "BO-SCAN-ACC"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "BO-SCAN-ACC",
				"item_name": "Serialized Optic", "item_group": "Accessory",
				"stock_uom": "Nos", "is_stock_item": 1, "has_serial_no": 1,
			}).insert(ignore_permissions=True)
		serial = self._stocked_serial("SS-SCAN-ACC-1", item_code="BO-SCAN-ACC")
		with self.assertRaises(frappe.ValidationError) as cm:
			manual_order.find_serial_for_order(serial)
		self.assertIn("not a firearm", str(cm.exception))


class TestLegacyStampPatch(PendingShipFixtures):
	def test_patch_stamps_only_fully_disposed_unpushed(self):
		from ffl_core.patches.stamp_legacy_shipstation_shipped import execute

		disposed = self._ship(self._stocked_serial("SS-PATCH-1"), update_stock=1)
		pending = self._ship(self._stocked_serial("SS-PATCH-2"), update_stock=0)
		pushed = self._ship(self._stocked_serial("SS-PATCH-3"), update_stock=1)
		frappe.db.set_value("Sales Invoice", pushed.name,
			"shipstation_order_id", "se-777", update_modified=False)

		execute()

		self.assertEqual(frappe.db.get_value(
			"Sales Invoice", disposed.name, "shipstation_status"), "Shipped")
		self.assertFalse(frappe.db.get_value(
			"Sales Invoice", disposed.name, "shipstation_order_id"),
			"the patch must never fabricate a ShipStation order id")
		self.assertEqual(frappe.db.get_value(
			"Sales Invoice", pending.name, "shipstation_status"), "Not Requested")
		self.assertEqual(frappe.db.get_value(
			"Sales Invoice", pushed.name, "shipstation_status"), "Not Requested")
