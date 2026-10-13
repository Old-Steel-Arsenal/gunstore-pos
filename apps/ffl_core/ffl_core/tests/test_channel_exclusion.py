"""A gun another channel has sold is refused by every gate, with real serials.

The sibling file (`test_channel_reservation_readpoints`) asserts that each gate
CALLS the predicate. This one asserts the answer is acted on — a gate that
consults it and then carries on regardless would pass that file and sell a gun
twice.

One flag, `gb_reserved`, stands for the whole class: it is the second channel,
and the second is what turns a hand-written `woo_reserved` test from adequate
into a hole. The consequence is the same for any future third — which is why
the flag is never named in the gates themselves.

Requires the GunBroker custom fields, so it skips on a bench that has not
migrated them. Skipping is the honest outcome there: without the column the
gates cannot be wrong in the way this file is looking for.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

DEALER = "_Test Channel Exclusion Dealer"
DEALER_FFL = "1-99-888-01-2D-00195"
ITEM = "CHAN-EXCL-GUN"
COST = 500.0
PRICE = 900.0
GB_ORDER_REF = "GB-ORD-TEST-0001"


class ChannelExclusionFixtures(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		cls.stores = frappe.db.get_value(
			"Warehouse", {"company": cls.company, "warehouse_name": "Stores"}, "name")

		if not frappe.db.exists("Item", ITEM):
			frappe.get_doc({
				"doctype": "Item", "item_code": ITEM, "item_name": ITEM,
				"item_group": "Firearm", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1, "is_firearm": 1,
			}).insert(ignore_permissions=True)

		if not frappe.db.exists("Customer", DEALER):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": DEALER,
				"customer_group": "Commercial", "territory": "All Territories",
				"is_ffl_dealer": 1, "ffl_number": DEALER_FFL,
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("ATF FFL Record", DEALER_FFL):
			frappe.get_doc({
				"doctype": "ATF FFL Record", "ffl_number": DEALER_FFL,
				"licensee_name": "Channel Exclusion Dealer LLC",
				"street": "9 Test Way", "city": "Austin", "state": "TX", "zip": "78701",
				"expiry_date": add_days(nowdate(), 365),
			}).insert(ignore_permissions=True)

	def setUp(self):
		if not frappe.db.has_column("Serial No", "gb_reserved"):
			self.skipTest(
				"GunBroker custom fields are not migrated on this bench — without "
				"the column there is no second channel for these gates to miss")

	def _receive(self, serial: str) -> str:
		from ffl_core.firearm import serialized_receipt_row
		if frappe.db.exists("Serial No", serial) and frappe.db.get_value(
				"Serial No", serial, "status") == "Active":
			frappe.db.set_value("Serial No", serial,
				{"gb_reserved": 0, "gb_order": "", "woo_reserved": 0, "woo_order": ""})
			return serial
		se = frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Receipt",
			"company": self.company,
			"items": [serialized_receipt_row(
				item_code=ITEM, qty=1, warehouse=self.stores, basic_rate=COST,
				serial=serial)],
		})
		se.flags.ignore_permissions = True
		se.insert()
		se.submit()
		frappe.db.set_value("Serial No", serial, "sell_price", PRICE)
		return serial

	def _sold_on_gunbroker(self, serial: str) -> str:
		"""Exactly what the order poll writes when a marketplace sale lands."""
		self._receive(serial)
		frappe.db.set_value("Serial No", serial,
			{"gb_reserved": 1, "gb_order": GB_ORDER_REF}, update_modified=False)
		return serial


class ThePosPickersDoNotOfferIt(ChannelExclusionFixtures):
	"""The gap this closes predates GunBroker.

	The pickers excluded counter holds, consigned-out guns and custody guns, and
	no channel reservation at all — so a rifle a paid WEB order already owned was
	still offered to the cashier. The owner's decision (2026-08-26) is that all
	channels are excluded, and staff release the gun on its own channel first.
	"""

	def test_the_shared_rulebook_excludes_it(self):
		from ffl_core.firearm import _pos_picker_excluded_serials
		free = self._receive("CHAN-EXCL-FREE-1")
		sold = self._sold_on_gunbroker("CHAN-EXCL-GBSOLD-1")
		excluded = _pos_picker_excluded_serials([ITEM])
		self.assertIn(sold, excluded)
		self.assertNotIn(free, excluded)

	def test_all_three_pickers_drop_it(self):
		from ffl_core.firearm import (
			available_serials_for_items,
			available_serials_with_prices,
			in_stock_firearm_serials,
		)
		free = self._receive("CHAN-EXCL-FREE-2")
		sold = self._sold_on_gunbroker("CHAN-EXCL-GBSOLD-2")

		offered = available_serials_for_items([ITEM]).get(ITEM, [])
		self.assertIn(free, offered)
		self.assertNotIn(sold, offered)

		priced = [r["serial"] for r in
			available_serials_with_prices([ITEM]).get(ITEM, [])]
		self.assertIn(free, priced)
		self.assertNotIn(sold, priced)

		in_stock = [r["serial"] for r in in_stock_firearm_serials()]
		self.assertNotIn(sold, in_stock)

	def test_a_web_reservation_is_excluded_the_same_way(self):
		"""The hole as it existed before any of this: one channel, still missed."""
		from ffl_core.firearm import _pos_picker_excluded_serials
		serial = self._receive("CHAN-EXCL-WOOSOLD-1")
		frappe.db.set_value("Serial No", serial,
			{"woo_reserved": 1, "woo_order": "WOO-ORD-TEST-1"}, update_modified=False)
		self.assertIn(serial, _pos_picker_excluded_serials([ITEM]))


class TheCounterScanRefusesIt(ChannelExclusionFixtures):
	def test_scanning_it_names_the_channel_and_the_order(self):
		"""Staff have to know WHERE to release it, so the message says which
		channel and which order rather than "reserved"."""
		from ffl_core.api.manual_order import _serial_status
		serial = self._sold_on_gunbroker("CHAN-EXCL-GBSOLD-3")
		line, reason = _serial_status(serial)
		self.assertIsNone(line)
		self.assertIn("gunbroker", reason.lower())
		self.assertIn(GB_ORDER_REF, reason)

	def test_a_free_gun_still_scans(self):
		from ffl_core.api.manual_order import _serial_status
		serial = self._receive("CHAN-EXCL-FREE-3")
		line, reason = _serial_status(serial)
		self.assertIsNone(reason)
		self.assertEqual(line["serial"], serial)


class ThePredicateItself(ChannelExclusionFixtures):
	def test_it_reports_the_channel_and_the_order(self):
		from ffl_core.reservations import channel_reservation
		serial = self._sold_on_gunbroker("CHAN-EXCL-GBSOLD-6")
		held = channel_reservation(serial)
		self.assertEqual(held["channel"], "gunbroker")
		self.assertEqual(held["ref"], GB_ORDER_REF)

	def test_a_narrow_select_is_not_read_as_not_held(self):
		"""The subtle one, and the reason `row` handling is what it is.

		A caller that selected a short column list did not ask about the second
		channel. Treating "not asked" as "not held" is how a partial SELECT turns
		into a double sale, so the predicate reads the missing column rather than
		assuming.
		"""
		from ffl_core.reservations import channel_reservation
		serial = self._sold_on_gunbroker("CHAN-EXCL-GBSOLD-7")
		held = channel_reservation(serial, row={"woo_reserved": 0, "woo_order": ""})
		self.assertIsNotNone(held, "a column the caller did not select must be read")
		self.assertEqual(held["channel"], "gunbroker")

	def test_the_shelf_state_says_unsellable_and_why(self):
		from ffl_core.reservations import serial_shelf_state
		serial = self._sold_on_gunbroker("CHAN-EXCL-GBSOLD-8")
		state = serial_shelf_state(serial)
		self.assertFalse(state["sellable"])
		self.assertEqual(state["reason"], "channel_reserved")
		self.assertEqual(state["detail"]["channel"], "gunbroker")

	def test_a_gun_gunbroker_may_already_have_sold_fails_closed(self):
		"""`gb_sold_unknown` is set when GunBroker stopped serving a listing and
		the same SKU appeared in its sold list. Every picker and every gate has to
		treat it as gone, because the alternative is selling somebody's gun."""
		if not frappe.db.has_column("Serial No", "gb_sold_unknown"):
			self.skipTest("gb_sold_unknown is not migrated on this bench")
		from ffl_core.reservations import serial_shelf_state
		serial = self._receive("CHAN-EXCL-DOUBT-1")
		frappe.db.set_value("Serial No", serial, "gb_sold_unknown", 1,
			update_modified=False)
		state = serial_shelf_state(serial)
		self.assertFalse(state["sellable"])
		self.assertEqual(state["reason"], "sold_unknown")

	def test_a_free_gun_is_sellable(self):
		from ffl_core.reservations import channel_reservation, serial_shelf_state
		serial = self._receive("CHAN-EXCL-FREE-4")
		self.assertIsNone(channel_reservation(serial))
		self.assertTrue(serial_shelf_state(serial)["sellable"])


class TheConsolidationJobIsNeverBlocked(FrappeTestCase):
	"""The counter gate must never fail a consolidation job.

	The gate hangs off `Sales Invoice.before_submit`, and the invoice a
	consolidation produces is submitted inside a background job minutes after
	the customer has left. A throw there does not stop a sale — it fails the job
	and leaves the day unconsolidated, a shape this store has been bitten by.

	The exemption reads `is_consolidated`, so the whole protection rests on ONE
	fact we do not own: that ERPNext sets that flag on the merged invoice BEFORE
	submitting it. Asserting our own `_exempt_invoice` short-circuits a
	hand-built stub (which the unit tests do) cannot see that fact at all — if
	ERPNext ever set the flag after submit, every unit test would stay green and
	the nightly close would start failing.

	So this reads ERPNext's own source and pins the ordering. It costs no
	fixtures, and `make ci-upgrade-test` — which re-runs the suite against a
	freshly pulled ERPNext — is exactly where it earns its place.
	"""

	def test_erpnext_flags_the_merged_invoice_before_submitting_it(self):
		import inspect
		import re

		try:
			from erpnext.accounts.doctype.pos_invoice_merge_log import (
				pos_invoice_merge_log as merge_log,
			)
		except ImportError:
			self.skipTest("erpnext is not importable in this environment")

		for func_name in ("process_merging_into_sales_invoice",
				"process_merging_into_credit_notes"):
			with self.subTest(path=func_name):
				func = getattr(merge_log.POSInvoiceMergeLog, func_name)
				source = inspect.getsource(func)
				flag = re.search(r"\.is_consolidated\s*=\s*1", source)
				submit = re.search(r"\.submit\(\)", source)
				self.assertIsNotNone(flag,
					f"{func_name} no longer sets is_consolidated — the GunBroker "
					f"counter gate's exemption reads that flag, and without it a "
					f"consolidation job can be failed by a marketplace conflict")
				self.assertIsNotNone(submit, f"{func_name} no longer submits")
				self.assertLess(flag.start(), submit.start(),
					f"{func_name} now submits BEFORE flagging the invoice as "
					f"consolidated — `before_submit` would see is_consolidated=0 "
					f"and the GunBroker gate would apply to a background job")

	def test_our_exemption_reads_the_flag_erpnext_sets(self):
		"""The other half of the same contract, stated where it can drift."""
		from ffl_integrations.gunbroker import doc_events

		doc = frappe._dict({"is_consolidated": 1, "items": []})
		self.assertTrue(doc_events._exempt_invoice(doc))
		self.assertFalse(doc_events._exempt_invoice(
			frappe._dict({"is_consolidated": 0, "is_return": 0, "items": []})))


class TheRefusalSentenceAtTheCounter(FrappeTestCase):
	"""`channel_hold_reason` — pure string work, so no fixtures and it runs
	anywhere.

	Two holds, two different actions, and getting the wording wrong is not
	cosmetic. Generalising the sentence to "reserved by {channel} order {ref}"
	when the second channel arrived produced, for an ordinary web order with no
	ref yet:

	    Serial SS-SCAN-WOO is reserved by woo order — — release it there first.

	Two em dashes where the order number should be, "woo" shown to a cashier who
	has only ever seen "web order", and `test_pending_ship` red.
	"""

	def _reason(self, channel, ref):
		from ffl_core.reservations import channel_hold_reason
		return channel_hold_reason("SN-1", {"channel": channel, "ref": ref})

	def test_a_web_order_keeps_the_wording_staff_already_know(self):
		message = self._reason("woo", "WOO-ORD-0009")
		self.assertIn("web order", message)
		self.assertIn("WOO-ORD-0009", message)
		self.assertIn("release it there", message)
		self.assertNotIn("woo order", message)

	def test_a_web_order_with_no_ref_prints_no_placeholder(self):
		"""The sentence already has an em dash; a second one standing in for the
		missing order number is what produced '— —'."""
		message = self._reason("woo", "")
		self.assertIn("web order", message)
		self.assertNotIn("— —", message)
		self.assertIn("release it there", message)

	def test_a_marketplace_hold_names_the_channel_and_does_not_say_release(self):
		"""There is no seller-side cancel on GunBroker, so "release it there"
		sends staff looking for a control that does not exist — the same
		conclusion `webhook._held_elsewhere_reason` reached for the web side."""
		message = self._reason("gunbroker", "GB-ORD-0007")
		self.assertIn("gunbroker", message)
		self.assertIn("GB-ORD-0007", message)
		self.assertNotIn("release it there", message)
		self.assertIn("Pick a different gun", message)

	def test_a_marketplace_hold_with_no_ref_prints_no_placeholder(self):
		message = self._reason("gunbroker", "")
		self.assertIn("gunbroker", message)
		self.assertNotIn("— —", message)

	def test_the_serial_is_always_named(self):
		for channel, ref in (("woo", "W-1"), ("woo", ""),
				("gunbroker", "G-1"), ("gunbroker", "")):
			with self.subTest(channel=channel, ref=ref):
				self.assertIn("SN-1", self._reason(channel, ref))
