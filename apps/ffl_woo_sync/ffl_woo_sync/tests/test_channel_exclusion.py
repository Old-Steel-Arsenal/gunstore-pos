"""What the web store does once it knows another channel has the gun.

The predicate is tested in ffl_core, with real serials. These tests answer the
next question — that each gate here ACTS on the answer, and acts differently
where the difference matters:

* eviction is refused for another channel and still allowed within Woo. A web
  order's placeholder is ours to move; a marketplace sale is not, and there is
  no cancel button on the seller's side to make it ours later;
* the message tells staff what to DO, and that differs. "Release it there" is
  right for a web order and actively misleading for a marketplace sale — it
  sends somebody looking for a control that does not exist;
* the push guard sits above `_relist`, so the form's own button cannot lift it.

The predicate is patched rather than driven through the database because what
is under test is the reaction. Whether the gate ASKS at all is asserted
structurally in ffl_core's `test_channel_reservation_readpoints`, and whether
the predicate answers correctly is asserted there too, against real rows.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_woo_sync.woocommerce import mapping, sync, webhook

ITEM = "ITEM-001"
SERIAL = "SN-ABC"
GB_HOLD = {"channel": "gunbroker", "ref": "GB-ORD-0007", "field": "gb_reserved"}
WOO_HOLD = {"channel": "woo", "ref": "WOO-ORD-0009", "field": "woo_reserved"}


class ANamedWebOrderIsRefused(FrappeTestCase):
	"""`_serial_availability` — the line names one specific gun by SKU.

	This is the double-sale main line: before the predicate it read
	`woo_reserved` alone, so a gun already sold on a marketplace sailed through
	and was reserved for a web buyer as well.
	"""

	def _availability(self, held):
		with patch("frappe.db.get_value", return_value={
					"item_code": ITEM, "status": "Active", "warehouse": "Stores",
					"is_transfer_gun": 0, "woo_reserved": 0, "woo_order": ""}), \
				patch.object(webhook, "custody_warehouse_names", return_value=set()), \
				patch.object(webhook, "_serial_identity_problem", return_value=("", "")), \
				patch.object(webhook, "channel_reserve_columns", return_value=[]), \
				patch.object(webhook, "channel_reservation", return_value=held):
			return webhook._serial_availability(SERIAL, ITEM)

	def test_a_marketplace_sale_blocks_it_and_names_the_channel(self):
		reason, blocking = self._availability(GB_HOLD)
		self.assertIn("gunbroker", reason)
		self.assertIn("GB-ORD-0007", reason)

	def test_a_marketplace_sale_yields_no_blocking_order_so_nothing_is_evicted(self):
		"""`blocking_order` is what authorises eviction, and eviction is a
		WITHIN-Woo move. Returning one here would offer up a paid marketplace sale
		as something to reassign."""
		_reason, blocking = self._availability(GB_HOLD)
		self.assertEqual(blocking, "")

	def test_a_web_hold_still_yields_its_order_so_eviction_can_run(self):
		"""The existing behaviour, unchanged: another web order's placeholder is
		ours to move, and that is what unstuck three deadlocked paid orders."""
		with patch("frappe.db.get_value", return_value={
					"item_code": ITEM, "status": "Active", "warehouse": "Stores",
					"is_transfer_gun": 0, "woo_reserved": 1,
					"woo_order": "WOO-ORD-0009"}), \
				patch.object(webhook, "custody_warehouse_names", return_value=set()), \
				patch.object(webhook, "_serial_identity_problem", return_value=("", "")), \
				patch.object(webhook, "channel_reserve_columns", return_value=[]), \
				patch.object(webhook, "channel_reservation", return_value=WOO_HOLD):
			reason, blocking = webhook._serial_availability(SERIAL, ITEM)
		self.assertTrue(reason)
		self.assertEqual(blocking, "WOO-ORD-0009")

	def test_a_free_gun_passes(self):
		reason, blocking = self._availability(None)
		self.assertEqual(reason, "")
		self.assertEqual(blocking, "")


class ThePlaceholderPickerSkipsIt(FrappeTestCase):
	def test_a_channel_held_gun_is_excluded_in_sql_not_after_the_fetch(self):
		"""Filtered in the query, like the counter holds beside it.

		A row dropped after the fetch eats a candidate slot, and the excluded gun
		is the oldest free one by construction — so five unusable guns at the
		front would answer "no stock" with free rifles sitting behind them.
		"""
		captured = {}
		with patch("ffl_core.reservations.counter_holds", return_value={}), \
				patch.object(webhook, "channel_reserved_serials",
					return_value={SERIAL: GB_HOLD}), \
				patch.object(webhook, "custody_warehouse_names", return_value=set()), \
				patch("frappe.get_all",
					side_effect=lambda _dt, **kw: captured.setdefault("filters",
						kw.get("filters")) and []):
			webhook._pick_oldest_unreserved_serial(ITEM)
		self.assertIn(SERIAL, captured["filters"]["name"][1])

	def test_the_locked_recheck_asks_about_every_channel(self):
		"""The SQL filter ran before the lock. An order poll landing in between
		would otherwise be overwritten by a web order taking the same gun."""
		locked_rows = []
		with patch("ffl_core.reservations.counter_holds", return_value={}), \
				patch.object(webhook, "channel_reserved_serials", return_value={}), \
				patch.object(webhook, "channel_reserve_columns", return_value=[]), \
				patch.object(webhook, "custody_warehouse_names", return_value=set()), \
				patch("frappe.get_all", return_value=[
					{"name": "SN-TAKEN", "warehouse": "Stores"},
					{"name": "SN-FREE", "warehouse": "Stores"}]), \
				patch("frappe.db.get_value", side_effect=[
					{"status": "Active", "is_transfer_gun": 0},
					{"status": "Active", "is_transfer_gun": 0}]) as locked, \
				patch.object(webhook, "channel_reservation",
					side_effect=lambda sn, **_kw: locked_rows.append(sn) or (
						GB_HOLD if sn == "SN-TAKEN" else None)):
			self.assertEqual(webhook._pick_oldest_unreserved_serial(ITEM), "SN-FREE")
		self.assertEqual(locked_rows, ["SN-TAKEN", "SN-FREE"])
		self.assertTrue(all(c.kwargs.get("for_update") for c in locked.call_args_list),
			"dropping for_update is how two orders end up holding one gun")


class EvictionRefusesAPaidMarketplaceSale(FrappeTestCase):
	def _evict(self, holders, row=None):
		with patch("frappe.db.get_value", return_value=row if row is not None else {}), \
				patch.object(webhook, "channel_reserve_columns", return_value=[]), \
				patch.object(webhook, "channel_reservations", return_value=holders), \
				patch.object(webhook, "_log_error") as logged, \
				patch.object(webhook, "_evict_placeholder_hold",
					return_value="SN-REPLACEMENT") as evict:
			result = webhook._evict_blocking_hold(SERIAL, ITEM, new_owner="WOO-ORD-1")
		return result, evict, logged

	def test_it_will_not_reassign_a_gun_another_channel_sold(self):
		result, evict, logged = self._evict([GB_HOLD])
		self.assertIsNone(result)
		self.assertFalse(evict.called, "a marketplace sale is not ours to move")
		self.assertTrue(logged.called)

	def test_it_still_moves_another_web_orders_placeholder(self):
		result, evict, _logged = self._evict(
			[WOO_HOLD], row={"woo_reserved": 1, "woo_order": "WOO-ORD-0009"})
		self.assertEqual(result, "SN-REPLACEMENT")
		self.assertTrue(evict.called)

	def test_a_gun_carrying_BOTH_flags_is_still_refused(self):
		"""The ordering trap. Woo is first in the flag table, so a gun somehow
		holding both would report as held-by-woo to a first-hit test, pass the
		"is it mine?" check, and be handed to another web order while GunBroker
		still had it.

		Both writers take the row lock, so the state should not arise. This is
		the guard that has to catch it if it ever does — and it costs one line.
		"""
		result, evict, _logged = self._evict(
			[WOO_HOLD, GB_HOLD],
			row={"woo_reserved": 1, "woo_order": "WOO-ORD-0009"})
		self.assertIsNone(result)
		self.assertFalse(evict.called)

	def test_a_free_gun_is_not_an_eviction_at_all(self):
		result, evict, _logged = self._evict([])
		self.assertIsNone(result)
		self.assertFalse(evict.called)


class ASuggestionNeverPointsAtASoldGun(FrappeTestCase):
	"""A "did you mean…?" is how a mistyped serial gets corrected onto a real
	gun. Pointing it at one somebody has already bought is the double sale
	arriving by way of a helpful hint."""

	def _suggest(self, held_map):
		with patch("frappe.get_all", return_value=[
					{"name": "SN-0001"}, {"name": "SN-0002"}]), \
				patch.object(webhook, "channel_reserved_serials", return_value=held_map):
			return webhook._suggest_instock_serial("SN0001", ITEM, "WOO-ORD-1")

	def test_a_gun_sold_elsewhere_is_not_suggested(self):
		self.assertIsNone(self._suggest({"SN-0001": GB_HOLD}))

	def test_a_free_gun_is_suggested(self):
		self.assertEqual(self._suggest({}), "SN-0001")

	def test_this_orders_own_placeholder_is_still_suggested(self):
		"""It is already ours; suggesting it is how the order gets its serial."""
		self.assertEqual(
			self._suggest({"SN-0001": {"channel": "woo", "ref": "WOO-ORD-1"}}),
			"SN-0001")


class TheStorefrontCountAndPayload(FrappeTestCase):
	def test_a_channel_reserved_gun_publishes_stock_zero(self):
		payload = mapping.build_serial_product_payload(
			item_code=ITEM, serial_no=SERIAL, item_name="Colt", description="",
			sell_price=900.0, channel_reserved=True)
		self.assertEqual(payload["stock_quantity"], 0)

	def test_the_default_keeps_every_existing_caller_unchanged(self):
		"""Item-level payloads have no serial to ask about, so omission must mean
		what it always meant."""
		payload = mapping.build_serial_product_payload(
			item_code=ITEM, serial_no=SERIAL, item_name="Colt", description="",
			sell_price=900.0)
		self.assertEqual(payload["stock_quantity"], 1)

	def test_the_free_pool_does_not_count_a_channel_held_gun(self):
		with patch("ffl_core.reservations.counter_holds", return_value={}), \
				patch("ffl_core.reservations.channel_reserved_serials",
					return_value={"SN-B": GB_HOLD}), \
				patch("ffl_core.reservations.custody_serials", return_value=set()), \
				patch("frappe.get_all", return_value=["SN-A", "SN-B"]):
			self.assertEqual(mapping.free_units(ITEM), 1)


class ThePushGuard(FrappeTestCase):
	"""The one a single button press would otherwise defeat.

	`client_api.push_serial_now` passes `_relist=True`, so a guard placed under
	that flag would let one press of "Send to WooCommerce" put a gun already sold
	on GunBroker back on the storefront at stock 1.
	"""

	def _push(self, *, relist: bool):
		with patch.object(sync, "is_custody_serial", return_value=False), \
				patch.object(sync, "other_channel_reservation", return_value=GB_HOLD), \
				patch.object(sync, "_client") as client:
			result = sync.push_serial(SERIAL, _relist=relist)
		return result, client

	def test_it_refuses_and_builds_no_client(self):
		result, client = self._push(relist=False)
		self.assertEqual(result["skipped"], sync.CHANNEL_RESERVED)
		self.assertFalse(client.called, "a refusal must cost no HTTP and no config read")

	def test_relist_cannot_lift_it(self):
		result, _client = self._push(relist=True)
		self.assertEqual(result["skipped"], sync.CHANNEL_RESERVED)

	def test_the_refusal_says_which_channel_and_which_order(self):
		result, _client = self._push(relist=False)
		self.assertEqual(result["channel"], "gunbroker")
		self.assertEqual(result["ref"], "GB-ORD-0007")


class TheGuardBelievesThePayloadNotTheCaller(FrappeTestCase):
	"""The test is worded as "would this publish, or raise the stock" — a
	property of the OUTGOING PAYLOAD, not of the caller's name.

	The guard cannot read the payload: it runs before the client is built, so a
	refusal costs no HTTP. It stands in `pos_fields_only AND an existing product`
	instead, on the strength of an invariant produced in a different module
	(mapping sends a channel-held gun to draft at stock 0). Two modules, one
	invariant, no compiler between them — so the payload is asked again once it
	exists, and that second question is what these tests pin.
	"""

	def _push(self, *, product_id, payload, pos_fields_only=True):
		client = MagicMock()
		client.get_attribute_ids.return_value = {}
		client.update_product.return_value = {"id": 11}
		with patch.object(sync, "is_custody_serial", return_value=False), \
				patch.object(sync, "other_channel_reservation", return_value=GB_HOLD), \
				patch.object(sync, "effective_serial_price", return_value=900.0), \
				patch.object(sync, "is_sold_state", return_value=False), \
				patch.object(sync.mapping, "serial_to_product_payload",
					return_value=payload), \
				patch("frappe.db.get_value", side_effect=_serial_row_reader({
					"woo_product_id": product_id, "item_code": ITEM,
					"woo_delisted": 0, "status": "Active", "woo_sold_archived": 0,
				})), \
				patch("frappe.db.set_value"):
			return sync.push_serial(
				SERIAL, client=client, site="retail",
				pos_fields_only=pos_fields_only), client

	def _down(self):
		return mapping.build_serial_product_payload(
			item_code=ITEM, serial_no=SERIAL, item_name="Colt", description="",
			sell_price=900.0, channel_reserved=True)

	def test_a_machine_push_with_no_product_yet_is_a_create_and_is_refused(self):
		"""`lean` needs an existing product; without one this is a full CREATE.
		Minting a page for a gun another channel has sold is not a repair —
		there is nothing to take down."""
		result, client = self._push(product_id="", payload=self._down())
		self.assertEqual(result["skipped"], sync.CHANNEL_RESERVED)
		self.assertFalse(client.create_product.called)

	def test_a_payload_that_would_publish_is_refused_however_it_got_here(self):
		"""The backstop. Unreachable today — every path that produces a publish
		for a channel-held gun is filtered out upstream — which is exactly the
		kind of "cannot happen" that a later change to mapping turns into a
		gun somebody has already bought, back on the shelf."""
		payload = dict(self._down(), status="publish")
		result, client = self._push(product_id="11", payload=payload)
		self.assertEqual(result["skipped"], sync.CHANNEL_RESERVED)
		self.assertFalse(client.update_product.called)

	def test_a_payload_that_would_restock_is_refused_too(self):
		payload = dict(self._down(), stock_quantity=1)
		result, client = self._push(product_id="11", payload=payload)
		self.assertEqual(result["skipped"], sync.CHANNEL_RESERVED)
		self.assertFalse(client.update_product.called)

	def test_the_ordinary_take_it_down_push_still_goes_through(self):
		"""The reverse assertion, so the three above cannot pass by refusing
		everything."""
		result, client = self._push(product_id="11", payload=self._down())
		self.assertNotIn("skipped", result)
		self.assertTrue(client.update_product.called)


class TheRefusalTellsStaffWhatToDo(FrappeTestCase):
	"""Two holds, two different actions, so two different sentences.

	Another WEB order can let the gun go and POS re-applies the serial by itself,
	so staff are told to wait or pick another. A marketplace sale cannot be
	released at all — there is no seller-side cancel — so telling somebody to
	"release it there" sends them looking for a button that does not exist, and
	the only real action is to pick a different gun.
	"""

	def test_a_web_hold_says_it_can_be_released(self):
		message = webhook._held_elsewhere_reason(SERIAL, WOO_HOLD)
		self.assertIn("release it there", message)
		self.assertIn("WOO-ORD-0009", message)

	def test_a_marketplace_sale_says_to_pick_another_gun(self):
		message = webhook._held_elsewhere_reason(SERIAL, GB_HOLD)
		self.assertNotIn("release it there", message)
		self.assertIn("pick a different gun", message)
		self.assertIn("gunbroker", message)
		self.assertIn("GB-ORD-0007", message)


class AStaffTypedSerialOnAWebOrder(FrappeTestCase):
	"""`_apply_dealer_serial` — staff enter the gun they physically picked.

	The gun is refused outright when another CHANNEL has it: eviction exists to
	move a placeholder within Woo, and there is no placeholder to move here.
	"""

	def _apply(self, held):
		order = frappe._dict({"name": "WOO-ORD-1", "serials": []})
		row = frappe._dict({"item_code": ITEM, "confirmed_serial": "",
			"stock_hold_serial": ""})
		with patch.object(webhook, "_lock_item_reservations"), \
				patch.object(webhook, "channel_reserve_columns", return_value=[]), \
				patch("frappe.db.get_value", return_value={
					"item_code": ITEM, "status": "Active"}), \
				patch.object(webhook, "_serial_identity_problem", return_value=("", "")), \
				patch.object(webhook, "channel_reservation", return_value=held), \
				patch.object(webhook, "_reject_serial_row",
					side_effect=lambda _o, _r, reason, **_kw: reason) as reject, \
				patch.object(webhook, "_reserve_serial") as reserve:
			result = webhook._apply_dealer_serial(order, row, SERIAL)
		return result, reject, reserve

	def test_a_gun_sold_on_another_channel_is_rejected_not_evicted(self):
		result, reject, reserve = self._apply(GB_HOLD)
		self.assertTrue(reject.called)
		self.assertFalse(reserve.called, "the gun is not this order's to take")
		self.assertIn("gunbroker", result)

	def test_a_free_gun_is_pinned(self):
		_result, reject, reserve = self._apply(None)
		self.assertFalse(reject.called)
		self.assertTrue(reserve.called)


def _serial_row_reader(row: dict):
	"""A `frappe.db.get_value` double that answers per FIELD, not one value for
	everything. A blanket answer makes unrelated guards fire on a truthy string
	and the test then proves something nobody wrote.

	A DocType meta load (keyword call on "DocType", made whenever the meta cache
	is cold — a run that starts with this module, or follows a clear_cache())
	goes to the real database: it is not the read under test, and answering it
	from `row` crashed the test depending on which module ran before it."""
	real_get_value = frappe.db.get_value

	def _get_value(*args, **kw):
		doctype = args[0] if args else kw.get("doctype")
		if doctype == "DocType" or len(args) < 2:
			return real_get_value(*args, **kw)
		field = args[2] if len(args) > 2 else kw.get("field")
		if isinstance(field, list):
			return frappe._dict({f: row.get(f) for f in field})
		return row.get(field)
	return _get_value


class ReconcileHealsAGunSoldOnAnotherChannel(FrappeTestCase):
	"""The case the whole cross-channel change exists for, and the one that was
	silently broken.

	GunBroker sells a gun; the immediate delist fails on a WooCommerce 5xx;
	`woo_delisted` stays 0 and the storefront goes on offering stock 1. Every
	sweep, reconcile looks at that product, and what it does next is the
	difference between "fixed within the hour" and "buyable until a customer
	pays for a gun that is already sold".

	Two halves, and the second is the one an outcome-mocking test cannot see.
	The first asserts WHICH repair is chosen. The second runs the REAL
	`push_serial` — no stub over the guard — because the original bug was that
	the drift was detected correctly and then handed to a function that refused
	it. Every "assert push_serial was called" test in the suite reported that as
	green.
	"""

	def _row(self, *, channel_held: bool):
		return frappe._dict({
			"name": SERIAL, "item_code": ITEM, "woo_reserved": 0,
			"gb_reserved": 1 if channel_held else 0,
			"gb_order": "GB-ORD-0007" if channel_held else "",
		})

	def test_a_gun_another_channel_sold_is_repaired_by_taking_it_down(self):
		"""The routing half of the repair."""
		from ffl_woo_sync.woocommerce import tasks

		self.assertEqual(
			tasks._drift_repair(self._row(channel_held=True)),
			tasks.REPAIR_DELIST)

	def test_an_ordinary_drifted_gun_is_still_pushed(self):
		"""The regression half: nothing about the ordinary path changed."""
		from ffl_woo_sync.woocommerce import tasks

		self.assertEqual(
			tasks._drift_repair(self._row(channel_held=False)),
			tasks.REPAIR_PUSH)

	def test_the_machine_self_heal_actually_reaches_woocommerce(self):
		"""The half that caught the bug.

		The real `push_serial`, the real guard. A machine update for a gun
		another channel holds must get through and must carry a payload that can
		only take the product down — stock 0 and draft. An implementation whose
		guard refuses this leaves the storefront selling a sold gun forever, and
		it fails right here.
		"""
		client = MagicMock()
		client.get_attribute_ids.return_value = {}
		client.update_product.return_value = {"id": 11}
		with patch.object(sync, "is_custody_serial", return_value=False), \
				patch.object(sync, "effective_serial_price", return_value=900.0), \
				patch.object(sync, "is_sold_state", return_value=False), \
				patch.object(sync.mapping, "serial_to_product_payload",
					return_value=mapping.build_serial_product_payload(
						item_code=ITEM, serial_no=SERIAL, item_name="Colt",
						description="", sell_price=900.0, channel_reserved=True)), \
				patch("frappe.db.get_value", side_effect=_serial_row_reader({
					"woo_product_id": "11", "item_code": ITEM,
					# Exactly the state reconcile Pass 2 selects on: still listed,
					# NOT already delisted. A gun whose delist succeeded is
					# filtered out of Pass 2 upstream and never reaches here.
					"woo_delisted": 0, "status": "Active", "woo_sold_archived": 0,
					# The hold goes in as ROW DATA, so the guard's own predicate
					# has to get it right. Patching the predicate here would have
					# hidden the reverse bug (reading our own web order as
					# another channel's) just as thoroughly as patching the
					# guard hid this one.
					"woo_reserved": 0, "woo_order": "",
					"gb_reserved": 1, "gb_order": "GB-ORD-0007",
				})), \
				patch("frappe.db.set_value"):
			result = sync.push_serial(
				SERIAL, client=client, site="retail", pos_fields_only=True)

		self.assertNotIn("skipped", result,
			"the machine self-heal must not be refused — that is the bug")
		self.assertTrue(client.update_product.called)
		sent = client.update_product.call_args[0][1]
		self.assertEqual(sent["stock_quantity"], 0)
		self.assertEqual(sent["status"], "draft",
			"stock 0 while still published leaves a buyable-looking page")

	def test_the_manual_button_is_still_refused(self):
		"""The manual-button refusal survives the fix. The button passes `_relist=True` and NOT
		`pos_fields_only`, so it lands on the guard exactly as before."""
		client = MagicMock()
		with patch.object(sync, "is_custody_serial", return_value=False), \
				patch.object(sync, "other_channel_reservation", return_value=GB_HOLD):
			result = sync.push_serial(SERIAL, client=client, _relist=True)
		self.assertEqual(result["skipped"], sync.CHANNEL_RESERVED)
		self.assertFalse(client.update_product.called)


class TheDelistNetInPassThreeB(FrappeTestCase):
	"""Pass 3b's second chance, and why the channel arms belong in it.

	Pass 2 only looks at products whose WooCommerce state has DRIFTED from what
	POS says. A product sitting correctly at publish/stock-1 for a gun that has
	since been sold on another channel is not drift by any comparison Pass 2
	makes — POS said stock 1 when the page was written. So without an arm here,
	a delist that failed at the time of the sale never gets a second chance.
	"""

	def test_every_OTHER_channels_flag_is_an_arm_and_ours_is_not(self):
		"""Ours is not, and that omission is the load-bearing half.

		A `woo_reserved` gun is on a pending web order of ours — it belongs on
		the storefront, published at stock 0, until the order completes and the
		page becomes its SOLD archive. Fishing for it here drafts every gun being
		packed and stamps `woo_delisted=1`, after which `route_serial_exit`
		answers EXIT_SKIP at fulfilment and the archive page is gone. See
		`AWebOrderOfOurOwnIsNotAnotherChannel` for the rest of that chain.
		"""
		from ffl_woo_sync.woocommerce import tasks

		with patch("frappe.db.has_column", return_value=True):
			arms = tasks._delist_or_filters()
		self.assertEqual(arms["status"], ["!=", "Active"])
		self.assertEqual(arms["consigned_out"], 1)
		self.assertEqual(arms["is_transfer_gun"], 1)
		self.assertEqual(arms["gb_reserved"], 1)
		self.assertNotIn("woo_reserved", arms)

	def test_a_missing_column_drops_its_arm_rather_than_500ing(self):
		"""During a deploy the code is live before migrate has added the column,
		and every sweep runs through here."""
		from ffl_woo_sync.woocommerce import tasks

		with patch("frappe.db.has_column",
				side_effect=lambda _dt, col: col != "gb_reserved"):
			arms = tasks._delist_or_filters()
		self.assertNotIn("gb_reserved", arms)
		self.assertEqual(arms["status"], ["!=", "Active"],
			"the non-channel arms carry on regardless")


class AWebOrderOfOurOwnIsNotAnotherChannel(FrappeTestCase):
	"""The hottest path in this app, and the one the cross-channel net caught by
	mistake.

	`woo_reserved` says a customer on THIS store has paid and the gun is being
	packed. Its storefront shape is settled and old: stock 0, page still
	PUBLISHED, and when the order completes the page becomes the SOLD archive
	that the store's rankings live on. A gun on GunBroker is the opposite — it
	has left this store's shelf, and its page must go to draft.

	Reading our own flag as "another channel has it" makes every pending web
	order a delist. The damage is not the draft page, which the next push would
	undo; it is `woo_delisted=1`, because `route_serial_exit` answers EXIT_SKIP
	for anything already delisted and Pass 3a filters those rows out. The gun
	then completes its sale with no SOLD page and nothing left to notice.

	Nothing here patches the predicate or the guard. The flags go in as row data
	and the real code answers, because the bug being pinned was a wrong ANSWER
	from real code, which is exactly what a patched predicate hides.
	"""

	def _row(self, **flags):
		row = frappe._dict({
			"name": SERIAL, "item_code": ITEM,
			"woo_reserved": 0, "woo_order": "",
			"gb_reserved": 0, "gb_order": "",
		})
		row.update(flags)
		return row

	WEB = {"woo_reserved": 1, "woo_order": "WOO-ORD-0009"}
	MARKET = {"gb_reserved": 1, "gb_order": "GB-ORD-0007"}

	# ── Pass 2: which repair a drifted product gets ──────────────────────────

	def test_pass_two_still_pushes_a_gun_our_own_web_order_holds(self):
		from ffl_woo_sync.woocommerce import tasks

		self.assertEqual(
			tasks._drift_repair(self._row(**self.WEB)), tasks.REPAIR_PUSH,
			"a pending web order is mid-fulfilment here, not sold elsewhere")

	def test_pass_two_takes_down_a_gun_the_marketplace_holds(self):
		from ffl_woo_sync.woocommerce import tasks

		self.assertEqual(
			tasks._drift_repair(self._row(**self.MARKET)), tasks.REPAIR_DELIST)

	def test_both_flags_at_once_is_still_taken_down(self):
		"""Should not arise — both writers take the row lock — but if it does,
		the marketplace hold is the one that cannot be undone."""
		from ffl_woo_sync.woocommerce import tasks

		self.assertEqual(
			tasks._drift_repair(self._row(**self.WEB, **self.MARKET)),
			tasks.REPAIR_DELIST)

	# ── Pass 3b: what the net fishes for ─────────────────────────────────────

	# (Pass 3b's net is asserted where its shape lives —
	# `TheDelistNetInPassThreeB.test_every_OTHER_channels_flag_is_an_arm_and_ours_is_not`.)

	# ── The payload ──────────────────────────────────────────────────────────

	def _payload(self, **flags):
		serial = self._row(**flags)
		serial.update({
			"status": "Active", "sell_price": 900.0, "item_name": "Colt",
			"description": "", "image_gallery": [], "woo_sold_archived": 0,
		})
		item = frappe._dict({
			"name": ITEM, "item_name": "Colt", "description": "",
			"image_gallery": [], "standard_rate": 900.0,
		})
		docs = {"Serial No": serial, "Item": item}
		with patch("frappe.get_doc", side_effect=lambda dt, _n: docs[dt]):
			return mapping.serial_to_product_payload(SERIAL)

	def test_a_pending_web_order_keeps_a_published_page_at_stock_zero(self):
		payload = self._payload(**self.WEB)
		self.assertEqual(payload["stock_quantity"], 0)
		self.assertEqual(payload["status"], "publish",
			"drafting it 404s the page a paying customer is looking at, and "
			"costs the ranking the SOLD archive exists to keep")

	def test_a_marketplace_hold_goes_to_draft(self):
		payload = self._payload(**self.MARKET)
		self.assertEqual(payload["stock_quantity"], 0)
		self.assertEqual(payload["status"], "draft")

	# ── push_serial, the real guard ──────────────────────────────────────────

	def test_a_machine_push_for_a_pending_web_order_is_not_refused(self):
		"""The guard asks "is somebody ELSE holding it", so this must sail past
		it and reach WooCommerce publish-at-stock-0."""
		client = MagicMock()
		client.get_attribute_ids.return_value = {}
		client.update_product.return_value = {"id": 11}
		row = self._row(**self.WEB)
		with patch.object(sync, "is_custody_serial", return_value=False), \
				patch.object(sync, "effective_serial_price", return_value=900.0), \
				patch.object(sync, "is_sold_state", return_value=False), \
				patch.object(sync.mapping, "serial_to_product_payload",
					return_value=mapping.build_serial_product_payload(
						item_code=ITEM, serial_no=SERIAL, item_name="Colt",
						description="", sell_price=900.0, woo_reserved=True)), \
				patch("frappe.db.get_value", side_effect=_serial_row_reader({
					"woo_product_id": "11", "item_code": ITEM, "woo_delisted": 0,
					"status": "Active", "woo_sold_archived": 0, **row,
				})), \
				patch("frappe.db.set_value") as write:
			result = sync.push_serial(
				SERIAL, client=client, site="retail", pos_fields_only=True)

		self.assertNotIn("skipped", result)
		sent = client.update_product.call_args[0][1]
		self.assertEqual(sent["stock_quantity"], 0)
		self.assertEqual(sent["status"], "publish")
		self.assertNotIn("woo_delisted", write.call_args[0][2],
			"an ordinary push must never set the flag that forfeits the "
			"SOLD archive")

	# ── The downstream the delist would have cost ────────────────────────────

	def test_it_still_earns_its_sold_archive_page_when_the_order_completes(self):
		"""`route_serial_exit`'s second rule is "sold AND delisted -> skip". A
		gun wrongly delisted while its web order was in flight arrives at
		fulfilment already carrying the flag, and this is where the archive is
		lost — silently, and with no row left for Pass 3a to rescue."""
		with patch("frappe.db.get_value", side_effect=_serial_row_reader(
				{"woo_delisted": 0})):
			self.assertEqual(
				sync.route_serial_exit(SERIAL, status="Delivered", site="retail"),
				sync.EXIT_ARCHIVE)

	def test_and_would_have_lost_it_had_the_sweep_delisted_it(self):
		"""The other half of the same fact, so the first test cannot pass for an
		unrelated reason."""
		with patch("frappe.db.get_value", side_effect=_serial_row_reader(
				{"woo_delisted": 1})):
			self.assertEqual(
				sync.route_serial_exit(SERIAL, status="Delivered", site="retail"),
				sync.EXIT_SKIP)
