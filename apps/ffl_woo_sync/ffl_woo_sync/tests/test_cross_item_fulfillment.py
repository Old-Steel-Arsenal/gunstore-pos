"""A staff serial from a declared sister pool stops being an error.

The store lists one uniform-price product (`Mosin 91/30`) but keeps a second,
per-gun-priced Item holding the same model (`M91/30`). Staff routinely fulfil a
uniform-price web order with a rifle out of that per-gun pool — 19 hand
re-bucketings in the three days around 2026-08-13. Every one of them started as
a refused serial, because `_apply_dealer_serial` requires the entered serial's
`Serial No.item_code` to equal the order line's.

That check has to stay: `dispose_web_order` books `FFL Disposition{item: <order
line item>, serial: <confirmed serial>}` and its `on_submit` issues the stock,
and ERPNext ties a serial to exactly one Item. Accepting the mismatch here only
moves the failure to disposition time, after the gun is packed.

So two things change, and neither is "accept a mismatched serial":

* the refusal says WHICH of three different things went wrong. The old `or`
  chain collapsed "no such serial" / "already sold" / "filed under another Item"
  into one sentence that reads like the gun does not exist — while it sat on the
  shelf. Only the third has a one-click fix, and it was the one the message hid.
* an Item may DECLARE that its serials also sell through a listing Item
  (`Item.sells_through_item`). For a declared pair the mismatch is the store's
  normal picking flow, so POS re-buckets the gun and applies the serial itself.
  Undeclared mismatches still fail loudly — a typo that happens to name a real
  gun in some other Item must never silently move that gun.

The re-bucket is ENQUEUED, never inline: `reassign_serial_to_item` submits two
Stock Entries and commits, and this module never commits — every outbound effect
rides `enqueue_after_commit`, and committing mid-webhook would drop the
`for_update` locks held over a half-processed order.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

WEBHOOK = "ffl_woo_sync.woocommerce.webhook"
POOL = "QA-XIF-POOL"          # per-gun-priced pool
LISTING = "QA-XIF-LISTING"    # uniform-price listing it sells through
STRANGER = "QA-XIF-STRANGER"  # declares nothing


def _srow(item_code=LISTING, confirmed_serial="", stock_hold_serial="",
		reservation_error=""):
	return frappe._dict({
		"name": "row0001", "item_code": item_code,
		"confirmed_serial": confirmed_serial,
		"stock_hold_serial": stock_hold_serial, "woo_line_id": "7",
		"unit_index": 0, "reservation_error": reservation_error,
	})


def _order(name="WOO-ORD-00001", woo_order_id="4951"):
	o = MagicMock()
	o.name = name
	o.woo_order_id = woo_order_id
	return o


def _sn(item_code=POOL, status="Active", woo_reserved=0, woo_order=""):
	return {"item_code": item_code, "status": status,
			"woo_reserved": woo_reserved, "woo_order": woo_order}


# ---------------------------------------------------------------------------
# _serial_identity_problem — three failures must not share one sentence
# ---------------------------------------------------------------------------

class TestSerialIdentityProblem(FrappeTestCase):
	"""The kind matters as much as the text: `_apply_dealer_serial` branches on
	it (only `wrong_item` may re-bucket, only the others take a typo suggestion),
	so a wrong kind is a wrong action, not just a wrong message."""

	def _problem(self, sn, item_code=LISTING, serial="RMN092762"):
		from ffl_woo_sync.woocommerce.webhook import _serial_identity_problem
		return _serial_identity_problem(serial, sn, item_code)

	def test_serial_matching_item_and_active_is_no_problem(self):
		kind, reason = self._problem(_sn(item_code=LISTING))
		self.assertEqual(kind, "")
		self.assertEqual(reason, "")

	def test_unknown_serial_says_it_does_not_exist(self):
		kind, reason = self._problem(None)
		self.assertEqual(kind, "unknown")
		self.assertIn("RMN092762", reason)
		# Must NOT send staff to Reassign Item — there is nothing to reassign.
		self.assertNotIn("Reassign", reason)

	def test_inactive_serial_names_the_status(self):
		kind, reason = self._problem(_sn(item_code=LISTING, status="Delivered"))
		self.assertEqual(kind, "inactive")
		self.assertIn("Delivered", reason)
		self.assertNotIn("Reassign", reason)

	def test_wrong_item_names_both_items_and_the_fix(self):
		"""The whole point: the gun IS in stock, and the message must say where
		it is filed and what one action puts it right."""
		kind, reason = self._problem(_sn(item_code=POOL))
		self.assertEqual(kind, "wrong_item")
		self.assertIn(POOL, reason)
		self.assertIn(LISTING, reason)
		self.assertIn("Reassign Item", reason)

	def test_sold_gun_in_another_item_reports_sold_not_reassign(self):
		"""Both wrong at once. 'Reassign it' would be actively bad advice for a
		gun that has already left — status is checked first for that reason."""
		kind, reason = self._problem(_sn(item_code=POOL, status="Delivered"))
		self.assertEqual(kind, "inactive")
		self.assertNotIn("Reassign", reason)

	def test_label_is_carried_into_the_text(self):
		from ffl_woo_sync.woocommerce.webhook import _serial_identity_problem
		_, reason = _serial_identity_problem(
			"SN-1", None, LISTING, label="Dealer-entered serial")
		self.assertTrue(reason.startswith("Dealer-entered serial"))


# ---------------------------------------------------------------------------
# _apply_dealer_serial — declared pools re-bucket, everything else still fails
# ---------------------------------------------------------------------------

class TestApplyDealerSerialCrossItem(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# _apply stubs frappe.db.get_value; the path under test reads the Serial
		# No meta (reservations.channel_reserve_columns), and a cold meta cache
		# would build that Meta from the stubbed value. Warm it first, so the
		# test holds whichever worker / order it runs in.
		frappe.get_meta("Serial No")

	def _apply(self, *, sn, sells_through, row=None, serial="RMN092762"):
		"""Run _apply_dealer_serial with the two lookups it makes on the mismatch
		path stubbed, and hand back (changed, row, enqueue mock)."""
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = row if row is not None else _srow()
		with patch("frappe.db.get_value", return_value=sn), \
			 patch(f"{WEBHOOK}._sells_through", return_value=sells_through), \
			 patch(f"{WEBHOOK}._suggest_instock_serial", return_value=None), \
			 patch(f"{WEBHOOK}._queue_wc_order_note"), \
			 patch(f"{WEBHOOK}._reserve_serial") as mock_reserve, \
			 patch(f"{WEBHOOK}._log_error"), \
			 patch("frappe.enqueue") as mock_enqueue:
			changed = _apply_dealer_serial(_order(), row, serial)
		return changed, row, mock_enqueue, mock_reserve

	def test_declared_pool_enqueues_the_rebucket_and_does_not_reject(self):
		changed, row, enqueue, reserve = self._apply(
			sn=_sn(item_code=POOL), sells_through=LISTING)
		self.assertTrue(changed)
		enqueue.assert_called_once()
		self.assertEqual(
			enqueue.call_args[0][0],
			"ffl_woo_sync.woocommerce.tasks.rebucket_and_apply_serial_task")
		kwargs = enqueue.call_args[1]
		self.assertEqual(kwargs["serial_no"], "RMN092762")
		self.assertEqual(kwargs["target_item"], LISTING)
		self.assertEqual(kwargs["woo_online_order"], "WOO-ORD-00001")
		# Carried so a failed move can re-flag THIS row instead of leaving the
		# reassuring in-flight note standing over a move that never happened.
		self.assertEqual(kwargs["serial_row_name"], "row0001")
		self.assertTrue(kwargs["enqueue_after_commit"])
		# The gun is not this order's until the move lands: nothing is reserved
		# and nothing is confirmed inside the webhook transaction.
		reserve.assert_not_called()
		self.assertEqual(row.confirmed_serial, "")

	def test_the_in_flight_note_is_not_an_error_message(self):
		"""It lands in `reservation_error`, which the Pending Order page shows to
		staff. Telling them to reconcile something POS is already fixing is how a
		second person starts hand-moving the same gun."""
		_, row, _, _ = self._apply(sn=_sn(item_code=POOL), sells_through=LISTING)
		self.assertIn("RMN092762", row.reservation_error)
		self.assertNotIn("must reconcile", row.reservation_error)
		self.assertNotIn("Reassign Item", row.reservation_error)

	def test_undeclared_pool_is_still_refused_and_moves_nothing(self):
		"""A typo that happens to name a real gun in an unrelated Item must never
		move that gun."""
		changed, row, enqueue, _ = self._apply(
			sn=_sn(item_code=STRANGER), sells_through="")
		self.assertTrue(changed)
		enqueue.assert_not_called()
		self.assertIn(STRANGER, row.reservation_error)
		self.assertIn("Reassign Item", row.reservation_error)

	def test_declaration_pointing_at_a_different_listing_does_not_authorize(self):
		"""POOL sells through some OTHER listing, not the one on this order."""
		changed, row, enqueue, _ = self._apply(
			sn=_sn(item_code=POOL), sells_through="QA-XIF-SOMETHING-ELSE")
		self.assertTrue(changed)
		enqueue.assert_not_called()
		self.assertIn("Reassign Item", row.reservation_error)

	def test_declared_pool_but_gun_already_sold_is_refused(self):
		changed, row, enqueue, _ = self._apply(
			sn=_sn(item_code=POOL, status="Delivered"), sells_through=LISTING)
		self.assertTrue(changed)
		enqueue.assert_not_called()
		self.assertIn("Delivered", row.reservation_error)

	def test_declared_pool_but_gun_held_by_another_order_is_refused(self):
		"""Re-bucketing a gun another order is holding would hand it to two
		customers — the exact failure the cross-order guard exists to stop."""
		changed, row, enqueue, _ = self._apply(
			sn=_sn(item_code=POOL, woo_reserved=1, woo_order="WOO-ORD-99999"),
			sells_through=LISTING)
		self.assertTrue(changed)
		enqueue.assert_not_called()
		self.assertIn("WOO-ORD-99999", row.reservation_error)

	def test_gun_held_by_THIS_order_still_re_buckets(self):
		"""A retry of the same order re-delivers its own serial. Its own hold is
		not a conflict, and refusing here would strand the order forever."""
		changed, _, enqueue, _ = self._apply(
			sn=_sn(item_code=POOL, woo_reserved=1, woo_order="WOO-ORD-00001"),
			sells_through=LISTING)
		self.assertTrue(changed)
		enqueue.assert_called_once()

	def test_repeat_delivery_re_enqueues_but_does_not_dirty_the_order(self):
		"""order.updated fires many times per order, and the retry sweep re-enters
		here every couple of hours — that repeat is the only thing that recovers a
		job lost to a restarted worker, so the enqueue must NOT be suppressed. The
		row write is what gets guarded: a second identical note would dirty the
		order and re-post the WooCommerce note for no new information."""
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		row = _srow()
		with patch("frappe.db.get_value", return_value=_sn(item_code=POOL)), \
			 patch(f"{WEBHOOK}._sells_through", return_value=LISTING), \
			 patch(f"{WEBHOOK}._queue_wc_order_note"), \
			 patch(f"{WEBHOOK}._log_error"), \
			 patch("frappe.enqueue") as enqueue:
			first = _apply_dealer_serial(_order(), row, "RMN092762")
			second = _apply_dealer_serial(_order(), row, "RMN092762")
		self.assertTrue(first)
		self.assertFalse(second)
		self.assertEqual(enqueue.call_count, 2)

	def test_matching_item_never_reads_the_declaration(self):
		"""The common path must not pay for this feature — and an extra read here
		would shift every ordered-mock sequence in the existing suite."""
		from ffl_woo_sync.woocommerce.webhook import _apply_dealer_serial
		with patch("frappe.db.get_value", return_value=_sn(item_code=LISTING)), \
			 patch(f"{WEBHOOK}._sells_through") as sells_through, \
			 patch(f"{WEBHOOK}._reserve_serial"), \
			 patch(f"{WEBHOOK}._enqueue_serial_or_item_push"):
			_apply_dealer_serial(_order(), _srow(), "RMN092762")
		sells_through.assert_not_called()


# ---------------------------------------------------------------------------
# _sells_through — against a REAL database
# ---------------------------------------------------------------------------

class TestSellsThrough(FrappeTestCase):
	"""Asserting on a mocked get_value would pass for a helper that reads the
	wrong fieldname, and the fieldname is the whole contract with the fixture."""

	def _item(self, code, **extra):
		if frappe.db.exists("Item", code):
			return code
		group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		doc = frappe.get_doc({
			"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": group, "stock_uom": "Nos", "is_stock_item": 1, **extra})
		doc.flags.ignore_permissions = True
		doc.insert()
		return code

	def test_declared_item_returns_its_listing(self):
		from ffl_woo_sync.woocommerce.webhook import _sells_through
		self._item(LISTING)
		self._item(POOL, sells_through_item=LISTING)
		self.assertEqual(_sells_through(POOL), LISTING)

	def test_undeclared_item_returns_empty(self):
		from ffl_woo_sync.woocommerce.webhook import _sells_through
		self._item(STRANGER)
		self.assertEqual(_sells_through(STRANGER), "")

	def test_blank_item_code_returns_empty(self):
		from ffl_woo_sync.woocommerce.webhook import _sells_through
		self.assertEqual(_sells_through(""), "")


# ---------------------------------------------------------------------------
# rebucket_and_apply_serial_task
# ---------------------------------------------------------------------------

class TestRebucketAndApplySerialTask(FrappeTestCase):
	def _run(self, *, reassign=None, order_status="Pending Fulfillment",
			current_item=POOL, applied=True):
		from ffl_woo_sync.woocommerce import tasks
		order = MagicMock()
		order.name = "WOO-ORD-00001"
		reassign = reassign or MagicMock(return_value={"ok": True})
		# Two reads, in order: the order's status, then where the gun is filed now.
		with patch("frappe.get_attr", return_value=reassign), \
			 patch("frappe.set_user"), \
			 patch("frappe.db.get_value",
				 side_effect=[order_status, current_item]), \
			 patch("frappe.db.set_value") as set_value, \
			 patch("frappe.get_doc", return_value=order), \
			 patch(f"{WEBHOOK}._apply_serials_from_woo", return_value=applied) as apply_, \
			 patch("frappe.log_error") as log_error, \
			 patch("frappe.db.rollback") as rollback:
			tasks.rebucket_and_apply_serial_task(
				serial_no="RMN092762", target_item=LISTING,
				woo_online_order="WOO-ORD-00001", serial_row_name="row0001",
				site="retail")
		return frappe._dict(reassign=reassign, order=order, apply_=apply_,
			log_error=log_error, rollback=rollback, set_value=set_value)

	def test_moves_the_gun_then_re_applies_the_serial(self):
		r = self._run()
		r.reassign.assert_called_once_with("RMN092762", LISTING)
		r.apply_.assert_called_once()
		r.order.save.assert_called_once()
		r.log_error.assert_not_called()

	def test_a_gun_already_in_the_target_item_is_not_moved_again(self):
		"""The sweep re-enqueues this job every pass until the row pins.
		`reassign_serial_to_item` throws 'already under' on a no-op move, so
		without this check ordinary recovery becomes a stream of Error Logs."""
		r = self._run(current_item=LISTING)
		r.reassign.assert_not_called()
		r.apply_.assert_called_once()
		r.order.save.assert_called_once()
		r.log_error.assert_not_called()

	def test_a_cancelled_order_is_not_re_bucketed(self):
		"""The order can be cancelled between the webhook and this job. Moving a
		gun for it would leave the rifle in the wrong Item with nothing pointing
		at it."""
		r = self._run(order_status="Cancelled")
		r.reassign.assert_not_called()
		r.apply_.assert_not_called()
		r.order.save.assert_not_called()

	def test_a_failed_reassign_rolls_back_logs_and_re_flags_the_row(self):
		"""The row is carrying "POS is moving the gun, no action needed". If the
		move died, that note has to stop being displayed — otherwise the queue
		lies to the person who has to ship the rifle."""
		boom = MagicMock(side_effect=frappe.ValidationError("nope"))
		r = self._run(reassign=boom)
		r.apply_.assert_not_called()
		r.order.save.assert_not_called()
		r.rollback.assert_called_once()
		r.log_error.assert_called_once()
		r.set_value.assert_called_once()
		args = r.set_value.call_args[0]
		self.assertEqual(args[0], "Woo Online Order Serial")
		self.assertEqual(args[1], "row0001")
		self.assertEqual(args[2], "reservation_error")
		self.assertIn("could not move", args[3])

	def test_no_change_from_re_apply_skips_the_save(self):
		r = self._run(applied=False)
		r.order.save.assert_not_called()
