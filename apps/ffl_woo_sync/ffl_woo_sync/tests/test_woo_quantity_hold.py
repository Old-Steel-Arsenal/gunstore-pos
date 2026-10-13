"""POS stops guessing WHICH rifle a uniform-price web order gets.

A uniform-price firearm Item is one WooCommerce product standing for several
interchangeable rifles. At order time POS used to park the OLDEST free one on the
order row as a `stock_hold_serial` — a guess made with no idea which rifle staff
would actually pull off the rack. Staff then pick a gun and type its serial on
the WooCommerce order; that serial is the physical truth. When guess and pick
disagree — the normal case with several orders in flight — the cross-order
double-sell guard refuses the staff serial because "another order holds it", and
with no free stock the refusals chain into a deadlock nothing can break. Seven
Mosin orders, production, 2026-08-13/14.

So: stop guessing. A uniform-price line now takes an UNASSIGNED CLAIM — the row
records that this order owes one unit of the Item, and names no rifle. The claim
still consumes a unit, which is what keeps WooCommerce's stock honest; it just
does not pretend to know which one.

The claim is not an error state. `awaiting_serial` in the Pending Order queue is
derived from a blank `confirmed_serial` alone (fulfillment.py), so an unassigned
claim already reads correctly there as "waiting for the serial from WooCommerce",
with no error badge. Nothing in the queue needed changing.

Two things had to move with it, and both are load-bearing:

* `stock_for_item` counts a uniform-price firearm's stock as free (unreserved)
  Serial Nos. A claim reserves nothing, so the count has to subtract claims
  explicitly or the storefront advertises rifles that are already sold.
* the outbound stock push and the cancel-time release are both driven by
  ITERATING RESERVED SERIALS. With nothing reserved, neither fires — the database
  would be right and the storefront wrong until the next reconcile sweep, which
  is a worse oversell than the deadlock this replaces.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.tests.channel_query_doubles import get_all_double, warm_channel_meta

WEBHOOK = "ffl_woo_sync.woocommerce.webhook"
MAPPING = "ffl_woo_sync.woocommerce.mapping"
UNIFORM = "QA-QH-UNIFORM"
OTHER = "QA-QH-OTHER-ITEM"


def _order_doc():
	"""A pre-insert order doc, the shape `_process_lines` mutates."""
	doc = MagicMock()
	doc.name = None
	doc.woo_order_id = "9100"
	doc.lines = []
	doc.serials = []

	def _append(table, row):
		getattr(doc, table).append(frappe._dict(row))
		return frappe._dict(row)

	doc.append.side_effect = _append
	return doc


def _wc_line(sku=UNIFORM, qty=1, line_id=7, serials=None):
	meta = [{"key": "firearm_serials", "value": serials}] if serials else []
	return {"id": line_id, "sku": sku, "quantity": qty, "price": "500.00",
			"subtotal": "500", "total": "500", "total_tax": "0", "meta_data": meta}


def _ensure_items():
	"""Item masters for the claim fixtures, created INSIDE the test transaction.

	Deliberately not committed from `setUpClass`: an earlier cut did, and left
	`QA-QH-*` Items in the dev site for good — exactly what `docs/testing.md`'s
	same-site repeatability rule exists to stop. Per test, so the harness's own
	rollback takes them with it.
	"""
	group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
	for code in (UNIFORM, OTHER):
		if frappe.db.exists("Item", code):
			continue
		doc = frappe.get_doc({
			"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": group, "stock_uom": "Nos", "is_stock_item": 1,
		})
		doc.flags.ignore_permissions = True
		doc.insert()


def _make_claim_order(*, rows, order_status="Pending Fulfillment", site="retail",
		item_code=UNIFORM):
	"""A real Woo Online Order, so the claim query runs against the database."""
	doc = frappe.get_doc({
		"doctype": "Woo Online Order",
		"woo_order_id": f"QH-{frappe.generate_hash(length=8)}",
		"order_status": order_status, "site": site, "customer_name": "QA Claim",
		"serials": [{
			"item_code": r.get("item_code", item_code),
			"woo_line_id": "1", "unit_index": 0,
			"confirmed_serial": r.get("confirmed_serial", ""),
			"stock_hold_serial": r.get("stock_hold_serial", ""),
			"reservation_error": r.get("reservation_error", ""),
			"fulfillment": r.get("fulfillment", "local"),
		} for r in rows],
	})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc


# ---------------------------------------------------------------------------
# unassigned_web_claims — against a REAL database
# ---------------------------------------------------------------------------

class TestUnassignedWebClaims(FrappeTestCase):
	"""This count is subtracted straight from what WooCommerce is told is on the
	shelf. Asserting on its SQL text would pass just as happily for `WHERE 1=1`,
	so every case goes against real rows."""

	def setUp(self):
		super().setUp()
		_ensure_items()

	def _count(self):
		from ffl_woo_sync.woocommerce.mapping import unassigned_web_claims
		return unassigned_web_claims(UNIFORM)

	def test_a_blank_pending_local_row_is_a_claim(self):
		before = self._count()
		_make_claim_order(rows=[{}])
		self.assertEqual(self._count(), before + 1)

	def test_a_row_that_names_a_serial_is_not_a_claim(self):
		"""It already reserves that Serial No, so the first term excludes it —
		counting it here would subtract the same rifle twice."""
		before = self._count()
		_make_claim_order(rows=[{"confirmed_serial": "QH-C1"}])
		_make_claim_order(rows=[{"stock_hold_serial": "QH-H1"}])
		self.assertEqual(self._count(), before)

	def test_a_no_stock_row_still_claims_its_unit(self):
		"""The order still owes a paid unit. Excluding it reads identically today
		(the count is already 0 and clamps), but the moment a crate lands the
		listing would advertise the new rifles while this order still owes one —
		oversell on exactly the orders already stuck."""
		before = self._count()
		from ffl_woo_sync.woocommerce.webhook import _NO_STOCK_REASON
		_make_claim_order(rows=[{
			"reservation_error": f"{_NO_STOCK_REASON} for {UNIFORM!r} to hold stock"}])
		self.assertEqual(self._count(), before + 1)

	def test_a_rejected_staff_serial_still_claims_its_unit(self):
		before = self._count()
		_make_claim_order(rows=[{
			"reservation_error": "Dealer-entered serial 'X' is not a valid in-stock serial"}])
		self.assertEqual(self._count(), before + 1)

	def test_an_order_that_left_the_queue_claims_nothing(self):
		before = self._count()
		_make_claim_order(rows=[{}], order_status="Transferred")
		self.assertEqual(self._count(), before)

	def test_a_dropship_row_claims_nothing(self):
		"""A drop-shipped unit ships from the distributor's warehouse — it holds
		none of the store's own stock. Its rows are blank/blank by design, so
		without the lane filter every drop-ship order would subtract for ever."""
		before = self._count()
		_make_claim_order(rows=[{"fulfillment": "direct"}])
		_make_claim_order(rows=[{"fulfillment": "fds"}])
		self.assertEqual(self._count(), before)

	def test_a_to_store_row_claims_nothing(self):
		"""`to_store` is procured from the distributor into the store, not held off
		the shelf — so it must NOT reuse `_LOCALLY_DISPOSED_LANES`, which includes it."""
		before = self._count()
		_make_claim_order(rows=[{"fulfillment": "to_store"}])
		self.assertEqual(self._count(), before)

	def test_a_blank_lane_counts_as_local(self):
		"""Every row written before drop-ship existed carries no lane."""
		before = self._count()
		_make_claim_order(rows=[{"fulfillment": ""}])
		self.assertEqual(self._count(), before + 1)

	def test_a_whitespace_only_serial_is_blank(self):
		"""Python consumers compare `.strip()`ed values, so SQL must agree: a row
		whose serial columns hold only whitespace names no rifle and must still
		claim its unit, or it gets neither a reservation nor a claim — oversell.

		Honest about what pins what: MariaDB's `utf8mb4_*_ci` collations are PAD
		SPACE, so `'   ' = ''` compares true even without the `TRIM`. The TRIM is
		belt-and-braces for a future non-padding collation; it does NOT cover a tab
		either (MySQL's one-argument TRIM strips spaces only). This case therefore
		proves the OUTCOME, not the mechanism."""
		before = self._count()
		_make_claim_order(rows=[{"confirmed_serial": "   ", "stock_hold_serial": "  "}])
		self.assertEqual(self._count(), before + 1)

	def test_another_items_claim_is_not_counted(self):
		before = self._count()
		_make_claim_order(rows=[{"item_code": OTHER}], item_code=OTHER)
		self.assertEqual(self._count(), before)


# ---------------------------------------------------------------------------
# stock_for_item — the number WooCommerce is actually told
# ---------------------------------------------------------------------------

class TestStockForItemSubtractsClaims(FrappeTestCase):
	def _stock(self, *, free, claims):
		"""`free_units` also asks `channel_reserved_serials`, whose SELECT wants
		rows and not names — a blanket answer of `["S0", …]` reaches it and
		`row["name"]` raises. The double answers that query with nothing held
		(this class is about CLAIMS) and leaves the name list to the query it was
		written for."""
		from ffl_woo_sync.woocommerce import mapping
		warm_channel_meta()
		with patch.object(mapping, "unassigned_web_claims", return_value=claims), \
			 patch("frappe.db.get_value", return_value={
				 "is_firearm": 1, "item_group": "Rifle", "uniform_sell_price": 1}), \
			 patch("frappe.get_all",
				 side_effect=get_all_double([f"S{i}" for i in range(free)])), \
			 patch("ffl_core.reservations.custody_serials", return_value=set()), \
			 patch("ffl_core.reservations.counter_holds", return_value={}), \
			 patch("frappe.get_hooks", return_value=[]):
			return mapping.stock_for_item(UNIFORM)

	def test_claims_come_off_the_advertised_count(self):
		self.assertEqual(self._stock(free=5, claims=2), 3)

	def test_no_claims_leaves_the_count_untouched(self):
		self.assertEqual(self._stock(free=5, claims=0), 5)

	def test_the_count_clamps_at_zero(self):
		"""An oversold shelf must not push a negative: WooCommerce would store 0
		and every later reconcile sweep would read the difference as drift and
		re-push, for ever — the same trap the distributor branch clamps for."""
		self.assertEqual(self._stock(free=1, claims=4), 0)


# ---------------------------------------------------------------------------
# _process_lines — the guess, removed
# ---------------------------------------------------------------------------

class TestUniformLineTakesAnUnassignedClaim(FrappeTestCase):
	def _run(self, lines, *, stock=5, uniform=True):
		from ffl_woo_sync.woocommerce import webhook
		order = _order_doc()
		with patch.object(webhook, "is_regulated_firearm_item", return_value=True), \
			 patch.object(webhook, "is_firearm_item", return_value=True), \
			 patch.object(webhook, "_is_uniform_price_item", return_value=uniform), \
			 patch.object(webhook, "_free_units_for_claim", return_value=stock), \
			 patch.object(webhook, "unassigned_web_claims", return_value=0), \
			 patch.object(webhook, "_pick_oldest_unreserved_serial") as pick, \
			 patch.object(webhook, "_log_error"):
			to_reserve = webhook._process_lines(order, lines)
		return order, to_reserve, pick

	def test_no_rifle_is_named_and_none_is_reserved(self):
		order, to_reserve, pick = self._run([_wc_line()])
		row = order.serials[0]
		self.assertEqual(row["stock_hold_serial"], "")
		self.assertEqual(row["confirmed_serial"], "")
		self.assertEqual(row["reservation_error"], "")
		self.assertEqual(to_reserve, [])
		pick.assert_not_called()

	def test_the_row_is_still_written_so_the_order_cannot_look_actionless(self):
		"""A firearm unit with no `serials` row lets the order read as 'nothing for
		POS to do' and leave the queue while a gun still owes a bound-book entry."""
		order, _to_reserve, _pick = self._run([_wc_line()])
		self.assertEqual(len(order.serials), 1)
		self.assertEqual(order.serials[0]["item_code"], UNIFORM)

	def test_qty_two_takes_two_claims_and_names_nothing_twice(self):
		"""The old path called the picker once per unit with nothing marking the
		previous pick, so both units got the SAME rifle. Counting cannot do that."""
		order, to_reserve, pick = self._run([_wc_line(qty=2)], stock=5)
		self.assertEqual(len(order.serials), 2)
		self.assertEqual([r["stock_hold_serial"] for r in order.serials], ["", ""])
		self.assertEqual(to_reserve, [])
		pick.assert_not_called()

	def test_a_staff_serial_on_the_line_is_still_honoured(self):
		from ffl_woo_sync.woocommerce import webhook
		order = _order_doc()
		with patch.object(webhook, "is_regulated_firearm_item", return_value=True), \
			 patch.object(webhook, "is_firearm_item", return_value=True), \
			 patch.object(webhook, "_is_uniform_price_item", return_value=True), \
			 patch.object(webhook, "_serial_availability", return_value=("", "")), \
			 patch.object(webhook, "_free_units_for_claim") as free, \
			 patch.object(webhook, "_log_error"):
			to_reserve = webhook._process_lines(order, [_wc_line(serials=["SN-STAFF"])])
		self.assertEqual(order.serials[0]["confirmed_serial"], "SN-STAFF")
		self.assertEqual(to_reserve, ["SN-STAFF"])
		free.assert_not_called()   # a named rifle needs no availability question

	def test_a_per_serial_listing_still_auto_fills_from_its_sku(self):
		"""One product, one rifle, priced by serial — the SKU carries the serial,
		so there is nothing to guess and the old behaviour is exactly right."""
		from ffl_woo_sync.woocommerce import webhook
		order = _order_doc()
		with patch.object(webhook, "is_regulated_firearm_item", return_value=True), \
			 patch.object(webhook, "is_firearm_item", return_value=True), \
			 patch.object(webhook, "_is_uniform_price_item", return_value=False), \
			 patch.object(webhook, "_serial_availability", return_value=("", "")), \
			 patch.object(webhook, "_log_error"):
			to_reserve = webhook._process_lines(
				order, [_wc_line(sku="AR15::SN-XYZ", serials=["SN-XYZ"])])
		self.assertEqual(order.serials[0]["confirmed_serial"], "SN-XYZ")
		self.assertEqual(to_reserve, ["SN-XYZ"])

	def test_an_empty_shelf_still_warns(self):
		"""Losing the guess must not lose the oversell alarm: the picker used to be
		what discovered 'nothing free'. Counting has to keep saying it."""
		from ffl_woo_sync.woocommerce.webhook import _NO_STOCK_REASON
		order, to_reserve, _pick = self._run([_wc_line()], stock=0)
		self.assertTrue(
			order.serials[0]["reservation_error"].startswith(_NO_STOCK_REASON),
			order.serials[0]["reservation_error"])
		self.assertEqual(to_reserve, [])

	def test_claims_made_earlier_in_the_same_order_count_against_it(self):
		"""The order is not inserted yet, so the database cannot see its own claims.
		Two free rifles and three units means the third unit is short."""
		from ffl_woo_sync.woocommerce.webhook import _NO_STOCK_REASON
		order, _to_reserve, _pick = self._run([_wc_line(qty=3)], stock=2)
		self.assertEqual(order.serials[0]["reservation_error"], "")   # claim (free 2)
		self.assertEqual(order.serials[1]["reservation_error"], "")   # named (free 1)
		self.assertTrue(
			order.serials[2]["reservation_error"].startswith(_NO_STOCK_REASON))

	def test_spent_units_span_two_lines_of_the_same_item(self):
		"""Two WooCommerce lines can name one Item; the accumulator is per order."""
		from ffl_woo_sync.woocommerce.webhook import _NO_STOCK_REASON
		order, _to_reserve, _pick = self._run(
			[_wc_line(line_id=7), _wc_line(line_id=8), _wc_line(line_id=9)], stock=2)
		self.assertEqual(order.serials[0]["reservation_error"], "")
		self.assertEqual(order.serials[1]["reservation_error"], "")
		self.assertTrue(
			order.serials[2]["reservation_error"].startswith(_NO_STOCK_REASON))

	def test_a_dropship_row_is_untouched(self):
		from ffl_woo_sync.woocommerce import webhook
		order = _order_doc()
		with patch.object(webhook, "is_regulated_firearm_item", return_value=True), \
			 patch.object(webhook, "is_firearm_item", return_value=True), \
			 patch.object(webhook, "_line_source", return_value={
				"lane": "direct", "distributor": "RSR", "sku": "S"}), \
			 patch.object(webhook, "_is_uniform_price_item") as uni, \
			 patch.object(webhook, "_free_units_for_claim") as free:
			to_reserve = webhook._process_lines(order, [_wc_line()])
		self.assertEqual(order.serials[0]["fulfillment"], "direct")
		self.assertEqual(to_reserve, [])
		uni.assert_not_called()
		free.assert_not_called()


# ---------------------------------------------------------------------------
# The outbound push — the edge that stops firing when nothing is reserved
# ---------------------------------------------------------------------------

class TestClaimStillPushesStock(FrappeTestCase):
	"""Both push edges iterate RESERVED SERIALS. A claim reserves nothing, so
	without an item-level push the storefront keeps advertising a sold rifle until
	the next reconcile sweep — a worse oversell than the deadlock this replaces."""

	def test_process_lines_reports_the_items_that_took_a_claim(self):
		from ffl_woo_sync.woocommerce import webhook
		order = _order_doc()
		with patch.object(webhook, "is_regulated_firearm_item", return_value=True), \
			 patch.object(webhook, "is_firearm_item", return_value=True), \
			 patch.object(webhook, "_is_uniform_price_item", return_value=True), \
			 patch.object(webhook, "_free_units_for_claim", return_value=5), \
			 patch.object(webhook, "_log_error"):
			webhook._process_lines(order, [_wc_line(qty=2)])
		self.assertEqual(webhook._claimed_items(order), {UNIFORM})

	def test_a_named_serial_reports_no_claimed_item(self):
		from ffl_woo_sync.woocommerce import webhook
		order = _order_doc()
		with patch.object(webhook, "is_regulated_firearm_item", return_value=True), \
			 patch.object(webhook, "is_firearm_item", return_value=True), \
			 patch.object(webhook, "_is_uniform_price_item", return_value=True), \
			 patch.object(webhook, "_serial_availability", return_value=("", "")), \
			 patch.object(webhook, "_log_error"):
			webhook._process_lines(order, [_wc_line(serials=["SN-STAFF"])])
		self.assertEqual(webhook._claimed_items(order), set())

	def test_a_no_stock_row_still_reports_its_item(self):
		"""It claims a unit (see TestUnassignedWebClaims), so the count moved and
		WooCommerce has to hear about it."""
		from ffl_woo_sync.woocommerce import webhook
		order = _order_doc()
		with patch.object(webhook, "is_regulated_firearm_item", return_value=True), \
			 patch.object(webhook, "is_firearm_item", return_value=True), \
			 patch.object(webhook, "_is_uniform_price_item", return_value=True), \
			 patch.object(webhook, "_free_units_for_claim", return_value=0), \
			 patch.object(webhook, "_log_error"):
			webhook._process_lines(order, [_wc_line()])
		self.assertEqual(webhook._claimed_items(order), {UNIFORM})


# ---------------------------------------------------------------------------
# Antique auto-clear
# ---------------------------------------------------------------------------

class TestAntiqueAutoClearNeedsASerial(FrappeTestCase):
	"""An all-antique order auto-clears to Transferred once WooCommerce completes
	it. `_issue_antique_units` skips rows with no serial, so nothing is issued out
	of stock — yet revenue is booked. Under the old placeholder the orphaned
	reservation accidentally kept the rifle off the storefront; a claim evaporates
	the moment the order leaves Pending Fulfillment, so a SOLD antique would go
	back on sale."""

	def _order(self, **row):
		return frappe._dict({
			"order_status": "Pending Fulfillment", "woo_status": "completed",
			"dropship_status": "",
			"serials": [frappe._dict({
				"item_code": "QA-ANTIQUE",
				"confirmed_serial": row.get("confirmed_serial", ""),
				"stock_hold_serial": row.get("stock_hold_serial", ""),
			})],
		})

	def test_it_refuses_while_an_antique_row_names_no_gun(self):
		from ffl_woo_sync.woocommerce.webhook import _auto_clear_non_firearm_if_completed
		doc = self._order()
		with patch(f"{WEBHOOK}.is_antique_item", return_value=True):
			self.assertFalse(_auto_clear_non_firearm_if_completed(doc))
		self.assertEqual(doc.order_status, "Pending Fulfillment")

	def test_it_clears_once_the_antique_has_a_serial(self):
		from ffl_woo_sync.woocommerce.webhook import _auto_clear_non_firearm_if_completed
		doc = self._order(confirmed_serial="ANT-1")
		with patch(f"{WEBHOOK}.is_antique_item", return_value=True):
			self.assertTrue(_auto_clear_non_firearm_if_completed(doc))
		self.assertEqual(doc.order_status, "Transferred")

	def test_an_order_with_no_serial_rows_still_clears(self):
		"""A pure accessory order owes nothing and must keep falling straight
		through, exactly as before."""
		from ffl_woo_sync.woocommerce.webhook import _auto_clear_non_firearm_if_completed
		doc = frappe._dict({
			"order_status": "Pending Fulfillment", "woo_status": "completed",
			"dropship_status": "", "serials": []})
		self.assertTrue(_auto_clear_non_firearm_if_completed(doc))


# ---------------------------------------------------------------------------
# The last free unit — the one case where naming a rifle is not a guess
# ---------------------------------------------------------------------------

class TestLastUnitIsStillNamed(FrappeTestCase):
	"""With exactly one unit free there is no alternative to be wrong about, so
	POS names it. That matters: a claim reserves no Serial No, and the counter's
	own gate reads `woo_reserved` — without this the shop could sell the last
	rifle out from under a paid web order and strand it."""

	def _run(self, *, stock, picked="SN-LAST", claims=0):
		from ffl_woo_sync.woocommerce import webhook
		order = _order_doc()
		with patch.object(webhook, "is_regulated_firearm_item", return_value=True), \
			 patch.object(webhook, "is_firearm_item", return_value=True), \
			 patch.object(webhook, "_is_uniform_price_item", return_value=True), \
			 patch.object(webhook, "_free_units_for_claim", return_value=stock), \
			 patch.object(webhook, "unassigned_web_claims", return_value=claims), \
			 patch.object(webhook, "_pick_oldest_unreserved_serial",
				 return_value=picked) as pick, \
			 patch.object(webhook, "_log_error"):
			to_reserve = webhook._process_lines(order, [_wc_line()])
		return order, to_reserve, pick

	def test_the_last_unit_is_named_and_reserved(self):
		order, to_reserve, pick = self._run(stock=1)
		self.assertEqual(order.serials[0]["stock_hold_serial"], "SN-LAST")
		self.assertEqual(order.serials[0]["reservation_error"], "")
		self.assertEqual(to_reserve, ["SN-LAST"])
		pick.assert_called_once_with(UNIFORM)

	def test_two_free_units_are_still_left_unnamed(self):
		order, to_reserve, pick = self._run(stock=2)
		self.assertEqual(order.serials[0]["stock_hold_serial"], "")
		self.assertEqual(to_reserve, [])
		pick.assert_not_called()

	def test_a_named_last_unit_is_not_also_counted_as_a_claim(self):
		"""It reserves a Serial No, so `unassigned_web_claims` must not subtract it
		a second time — the count would come off twice for one rifle."""
		from ffl_woo_sync.woocommerce import webhook
		order, _to_reserve, _pick = self._run(stock=1)
		self.assertEqual(webhook._claimed_items(order), set())

	def test_losing_the_race_for_the_last_unit_reports_no_stock(self):
		"""The availability read is unlocked, so two webhooks can both see the last
		unit. The picker's own two-phase `for_update` settles it — and the loser
		must be told, not handed a claim for a rifle that is already gone."""
		from ffl_woo_sync.woocommerce.webhook import _NO_STOCK_REASON
		order, to_reserve, _pick = self._run(stock=1, picked=None)
		self.assertTrue(
			order.serials[0]["reservation_error"].startswith(_NO_STOCK_REASON),
			order.serials[0]["reservation_error"])
		self.assertEqual(to_reserve, [])


	def test_a_claimed_pool_is_never_named_out_of(self):
		"""`free` is a promise budget, not a rack count — `stock_for_item` already
		nets off other orders' claims. Seven rifles with six claims outstanding also
		reads `free == 1`, and picking "the oldest" out of seven is exactly the guess
		this change exists to stop making."""
		order, to_reserve, pick = self._run(stock=1, claims=6)
		self.assertEqual(order.serials[0]["stock_hold_serial"], "")
		self.assertEqual(order.serials[0]["reservation_error"], "")
		self.assertEqual(to_reserve, [])
		pick.assert_not_called()


# ---------------------------------------------------------------------------
# The antique exits — one rule, written at BOTH of them
# ---------------------------------------------------------------------------

ANTIQUE = "QA-QH-ANTIQUE"


def _antique_order(*, lane="local", confirmed="", hold="", extra=None):
	rows = [frappe._dict({
		"item_code": ANTIQUE, "confirmed_serial": confirmed,
		"stock_hold_serial": hold, "fulfillment": lane,
	})]
	rows += extra or []
	return frappe._dict({
		"order_status": "Pending Fulfillment", "woo_status": "completed",
		"dropship_status": "", "serials": rows,
	})


class TestAntiqueAutoClearGate(FrappeTestCase):
	def _clear(self, doc):
		from ffl_woo_sync.woocommerce.webhook import _auto_clear_non_firearm_if_completed
		with patch(f"{WEBHOOK}.is_antique_item", return_value=True):
			return _auto_clear_non_firearm_if_completed(doc)

	def test_a_local_antique_with_no_serial_holds_the_order(self):
		self.assertFalse(self._clear(_antique_order()))

	def test_a_hold_only_antique_still_holds_the_order(self):
		"""`antique._issue_antique_units` skips on `confirmed_serial` alone, so a
		row carrying only a placeholder would sail past a wider predicate and
		produce the very outcome this guard exists to prevent."""
		self.assertFalse(self._clear(_antique_order(hold="ANT-HOLD")))

	def test_a_named_antique_clears(self):
		doc = _antique_order(confirmed="ANT-1")
		self.assertTrue(self._clear(doc))
		self.assertEqual(doc.order_status, "Transferred")

	def test_a_dropship_antique_still_clears(self):
		"""A non-local row is blank/blank BY DESIGN — nothing here is ours to hold,
		and the stock-out skips those lanes anyway. Without the lane filter such an
		order fails this guard on every pass and hangs in the queue for ever."""
		for lane in ("direct", "fds"):
			with self.subTest(lane=lane):
				doc = _antique_order(lane=lane)
				self.assertTrue(self._clear(doc))
				self.assertEqual(doc.order_status, "Transferred")

	def test_a_to_store_antique_with_no_serial_holds_the_order(self):
		"""`to_store` IS ours — received into the store, then retired like any
		local unit."""
		self.assertFalse(self._clear(_antique_order(lane="to_store")))


# ---------------------------------------------------------------------------
# The two load-bearing push edges — mutation showed neither had a test
# ---------------------------------------------------------------------------

class TestCancelReleasesAndPushes(FrappeTestCase):
	"""A cancelled claim frees its unit by leaving `Pending Fulfillment`, but the
	release loop it rides on iterates RESERVED SERIALS and a claim has none — so
	without the item-level push the storefront keeps the unit spoken for until the
	next reconcile. `_RELEASE_STATUSES` includes `checkout-draft`, which fires when
	a shopper merely opens checkout, so this is a hot path.

	Against a REAL order, so the handler's own claim predicate is covered rather
	than mocked away — it is the third place in this app that has to agree on what
	a claim is, and the one the review flagged as untested."""

	def setUp(self):
		super().setUp()
		_ensure_items()

	def _cancel(self, order):
		from ffl_woo_sync.woocommerce import webhook
		with patch("ffl_woo_sync.woocommerce.refunds.alert_if_invoiced"), \
			 patch(f"{WEBHOOK}._log_error"), \
			 patch(f"{WEBHOOK}._push_item_stock_everywhere") as push, \
			 patch("frappe.enqueue"):
			webhook._handle_order_cancelled(
				{"id": str(order.woo_order_id), "status": "cancelled"})
		return push

	def test_a_cancelled_claim_pushes_its_item(self):
		order = _make_claim_order(rows=[{}])
		self._cancel(order).assert_called_once_with(UNIFORM)

	def test_the_cancelled_order_actually_leaves_the_queue(self):
		"""The push is only half of it — the claim itself is freed by the status
		write, because `unassigned_web_claims` is scoped to Pending Fulfillment."""
		from ffl_woo_sync.woocommerce.mapping import unassigned_web_claims
		order = _make_claim_order(rows=[{}])
		before = unassigned_web_claims(UNIFORM)
		self._cancel(order)
		self.assertEqual(unassigned_web_claims(UNIFORM), before - 1)
		self.assertEqual(
			frappe.db.get_value("Woo Online Order", order.name, "order_status"),
			"Cancelled")

	def test_a_cancelled_named_row_does_not_double_push(self):
		"""It reserves a Serial No, so the existing per-serial release loop already
		pushes it — pushing again by item would be a second, redundant job."""
		order = _make_claim_order(rows=[{"stock_hold_serial": "QH-CANCEL-1"}])
		self._cancel(order).assert_not_called()

	def test_a_cancelled_dropship_row_pushes_nothing(self):
		"""Blank/blank by design: it never held any of the store's stock."""
		for lane in ("direct", "fds", "to_store"):
			with self.subTest(lane=lane):
				order = _make_claim_order(rows=[{"fulfillment": lane}])
				self._cancel(order).assert_not_called()


class TestItemPushReachesTheStore(FrappeTestCase):
	"""One physical shelf. `_push_item_stock_everywhere` is what a claim rides on."""

	def _push(self, item_row):
		from ffl_woo_sync.woocommerce import webhook
		with patch("frappe.db.get_value", return_value=item_row), \
			 patch("frappe.enqueue") as enq:
			webhook._push_item_stock_everywhere(UNIFORM)
		return [c[1] for c in enq.call_args_list if "push_stock_task" in c[0][0]]

	def test_a_listed_item_pushes_the_retail_store_once(self):
		calls = self._push({"woo_product_id": "WC-1"})
		self.assertEqual([c.get("site") for c in calls], [None])

	def test_an_unlisted_item_pushes_nothing(self):
		self.assertEqual(self._push({"woo_product_id": ""}), [])


class TestClaimedItemsLaneFilter(FrappeTestCase):
	"""`_claimed_items` decides what both push edges announce. Its lane filter had
	no test, and a drop-ship row is blank/blank by design."""

	def _items(self, rows):
		from ffl_woo_sync.woocommerce import webhook
		return webhook._claimed_items(frappe._dict({"serials": [
			frappe._dict(r) for r in rows]}))

	def test_a_local_blank_row_is_claimed(self):
		self.assertEqual(self._items([{"item_code": UNIFORM, "confirmed_serial": "",
			"stock_hold_serial": "", "fulfillment": "local"}]), {UNIFORM})

	def test_a_blank_lane_reads_as_local(self):
		self.assertEqual(self._items([{"item_code": UNIFORM, "confirmed_serial": "",
			"stock_hold_serial": "", "fulfillment": ""}]), {UNIFORM})

	def test_non_local_lanes_are_not_claims(self):
		for lane in ("direct", "fds", "to_store"):
			with self.subTest(lane=lane):
				self.assertEqual(self._items([{"item_code": UNIFORM,
					"confirmed_serial": "", "stock_hold_serial": "",
					"fulfillment": lane}]), set())

	def test_a_row_naming_a_rifle_is_not_a_claim(self):
		for field in ("confirmed_serial", "stock_hold_serial"):
			with self.subTest(field=field):
				row = {"item_code": UNIFORM, "confirmed_serial": "",
					"stock_hold_serial": "", "fulfillment": "local", field: "SN-1"}
				self.assertEqual(self._items([row]), set())


class TestSpentUnitsAccounting(FrappeTestCase):
	"""Every branch that consumes a unit must say so, or the next unit of the same
	order is told the shelf is deeper than it is. Mutation showed the named and
	no-stock branches were unguarded and the rejected branch disagreed with the
	database, which counts its blank/blank row as a claim."""

	def _run(self, lines, *, stock, reason="", picked="SN-PICK"):
		from ffl_woo_sync.woocommerce import webhook
		order = _order_doc()
		with patch.object(webhook, "is_regulated_firearm_item", return_value=True), \
			 patch.object(webhook, "is_firearm_item", return_value=True), \
			 patch.object(webhook, "_is_uniform_price_item", return_value=True), \
			 patch.object(webhook, "_serial_availability", return_value=(reason, "")), \
			 patch.object(webhook, "_free_units_for_claim", return_value=stock), \
			 patch.object(webhook, "unassigned_web_claims", return_value=0), \
			 patch.object(webhook, "_pick_oldest_unreserved_serial", return_value=picked), \
			 patch.object(webhook, "_log_error"):
			to_reserve = webhook._process_lines(order, lines)
		return order, to_reserve

	def test_a_named_unit_is_spent(self):
		"""Pool of 2, two units, no staff serials: unit 0 claims (free 2), unit 1
		would name (free 1) — but the gate refuses once this order has spent
		anything, so it claims too. Either way the SECOND unit must not be told
		the shelf is still 2 deep."""
		order, to_reserve = self._run([_wc_line(qty=2)], stock=2)
		self.assertEqual([r["stock_hold_serial"] for r in order.serials], ["", ""])
		self.assertEqual(to_reserve, [])

	def test_a_no_stock_unit_is_spent(self):
		"""Three units against one rifle: the first names it, the other two must
		BOTH report no stock — not just the second."""
		from ffl_woo_sync.woocommerce.webhook import _NO_STOCK_REASON
		order, _to_reserve = self._run([_wc_line(qty=3)], stock=1)
		errs = [r["reservation_error"] for r in order.serials]
		self.assertEqual(errs[0], "")
		self.assertTrue(all(e.startswith(_NO_STOCK_REASON) for e in errs[1:]), errs)

	def test_a_rejected_staff_serial_is_spent(self):
		"""Its row is blank/blank, so `unassigned_web_claims` counts it as a claim
		once the order is inserted — the in-request budget has to agree.

		Shaped so the second unit is the one that answers: unit 0 carries a staff
		serial that is refused, unit 1 carries none and falls to the uniform branch
		against a shelf of one. If the refused unit did not spend its unit, unit 1
		would read `free == 1` and NAME that rifle — promising the same gun twice."""
		from ffl_woo_sync.woocommerce.webhook import _NO_STOCK_REASON
		order, to_reserve = self._run(
			[_wc_line(qty=2, serials=["SN-BAD"])], stock=1,
			reason="Serial 'SN-BAD' is not a valid in-stock serial")
		self.assertIn("SN-BAD", order.serials[0]["reservation_error"])
		self.assertTrue(
			order.serials[1]["reservation_error"].startswith(_NO_STOCK_REASON),
			order.serials[1]["reservation_error"])
		self.assertEqual(order.serials[1]["stock_hold_serial"], "")
		self.assertEqual(to_reserve, [])

	def test_a_confirmed_staff_serial_is_spent(self):
		"""Pool of 2, unit 0 named by staff, unit 1 blank. Unit 1 must NOT be
		handed the rifle unit 0 just confirmed — it is not reserved until after
		the insert, so the picker can still see it as free."""
		order, to_reserve = self._run(
			[_wc_line(qty=2, serials=["SN-STAFF"])], stock=2, picked="SN-STAFF")
		self.assertEqual(order.serials[0]["confirmed_serial"], "SN-STAFF")
		self.assertEqual(order.serials[1]["stock_hold_serial"], "",
			"the picker must not be able to return a rifle this order already took")
		self.assertEqual(to_reserve, ["SN-STAFF"])


class TestUniformGateReadsTheItem(FrappeTestCase):
	"""`_is_uniform_price_item` is patched everywhere else in this file; mutation
	showed nothing checks it actually reads the field that decides the whole
	branch."""

	def test_it_reads_uniform_sell_price(self):
		from ffl_woo_sync.woocommerce.webhook import _is_uniform_price_item
		with patch("frappe.db.get_value", return_value=1) as gv:
			self.assertTrue(_is_uniform_price_item(UNIFORM))
		self.assertEqual(gv.call_args[0][:3], ("Item", UNIFORM, "uniform_sell_price"))

	def test_a_per_serial_item_is_not_uniform(self):
		from ffl_woo_sync.woocommerce.webhook import _is_uniform_price_item
		with patch("frappe.db.get_value", return_value=0):
			self.assertFalse(_is_uniform_price_item(UNIFORM))


class TestPickerRulebook(FrappeTestCase):
	"""The picker is now the ONLY path that names a rifle, and the gate's argument
	rests on it naming out of the same pool the count measured."""

	def test_it_excludes_a_consigned_out_gun(self):
		"""Active, not woo_reserved, but sitting at a partner FFL — `stock_for_item`
		never counted it, so naming it would hand a web order a rifle the storefront
		does not have."""
		from ffl_woo_sync.woocommerce.webhook import _pick_oldest_unreserved_serial
		with patch("frappe.get_all", return_value=[]) as ga, \
			 patch(f"{WEBHOOK}.custody_warehouse_names", return_value=set()):
			_pick_oldest_unreserved_serial(UNIFORM)
		self.assertEqual(ga.call_args.kwargs["filters"].get("consigned_out"), 0)
