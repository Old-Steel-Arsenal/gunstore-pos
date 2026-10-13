"""Which document changes reach the GunBroker listing, and which must not.

Five documents are watched because ERPNext emits no single "this gun is spoken
for" event. That breadth is the risk as much as the point: too narrow and a gun
stays buyable on a marketplace after it has left the shelf; too broad and every
stock count queues a round trip.

So the tests come in pairs — the thing that must fire, and the neighbouring
thing that must not.

The Woo Online Order handler is the one that needs the most explaining, and it
has two tests for the two serial columns. A web order that NAMES a gun writes
`confirmed_serial`; one that merely claims a unit writes `stock_hold_serial`,
and the picker behind that placeholder excludes reserved guns but not LISTED
ones — being on GunBroker is not being reserved — so a placeholder lands on a
listed gun as a matter of course. A handler reading only `confirmed_serial`
would miss exactly that.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import doc_events, tasks

SYNC_TASK = "ffl_integrations.gunbroker.tasks.gb_sync_serial_listing_task"
REPRICE_TASK = "ffl_integrations.gunbroker.tasks.gb_reprice_serial_task"


class _EventCase(FrappeTestCase):
	def setUp(self):
		self.listed: set[str] = {"SN-LISTED"}
		self.enqueued: list[dict] = []
		self.written: dict = {}

		self._patch_object(doc_events, "_skip", return_value=False)
		self._patch("frappe.enqueue",
			side_effect=lambda path, **kw: self.enqueued.append({"path": path, **kw}))
		self._patch("frappe.db.get_value",
			side_effect=lambda _dt, name, field=None, **_kw: (
				"16087230" if field == "gb_item_id" and name in self.listed else None))
		self._patch("frappe.db.set_value",
			side_effect=lambda _dt, name, field, value=None, **_kw: self.written.update(
				field if isinstance(field, dict) else {field: value}))
		self._patch_object(doc_events, "serials_for_line",
			side_effect=lambda row: [row["serial_no"]] if row.get("serial_no") else [])

	def _patch(self, target, **kw):
		patcher = patch(target, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _patch_object(self, target, attr, **kw):
		patcher = patch.object(target, attr, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _serials_enqueued(self) -> list[str]:
		return [job["serial_no"] for job in self.enqueued if job["path"] == SYNC_TASK]


class StockMovements(_EventCase):
	def test_a_listed_gun_moving_reaches_the_listing(self):
		doc_events.on_sle_submit(frappe._dict({"serial_no": "SN-LISTED"}))
		self.assertEqual(self._serials_enqueued(), ["SN-LISTED"])

	def test_a_gun_that_is_not_listed_costs_nothing(self):
		"""Filtered here rather than in the task: a counter sale of five
		accessories and one unlisted rifle must not queue six pointless jobs."""
		doc_events.on_sle_submit(frappe._dict({"serial_no": "SN-OTHER"}))
		self.assertEqual(self.enqueued, [])

	def test_a_cancelled_movement_is_treated_the_same(self):
		"""A reversal changes the answer just as the movement did."""
		doc_events.on_sle_cancel(frappe._dict({"serial_no": "SN-LISTED"}))
		self.assertEqual(self._serials_enqueued(), ["SN-LISTED"])


class CounterInvoices(_EventCase):
	def _invoice(self, *serials, **flags):
		doc = frappe._dict({
			"is_consolidated": 0, "is_return": 0,
			"items": [frappe._dict({"serial_no": s}) for s in serials]})
		doc.update(flags)
		return doc

	def test_parking_a_gun_on_a_counter_order_ends_its_listing(self):
		"""Stock has not moved — the gun sits on its shelf waiting for payment and
		a 4473 — but it is promised, and a promised gun must come off GunBroker
		now rather than at the next hourly sweep."""
		doc_events.on_invoice_change(self._invoice("SN-LISTED"))
		self.assertEqual(self._serials_enqueued(), ["SN-LISTED"])

	def test_a_consolidated_invoice_is_ignored(self):
		"""It is a background job re-recording sales that already happened."""
		doc_events.on_invoice_change(self._invoice("SN-LISTED", is_consolidated=1))
		self.assertEqual(self.enqueued, [])

	def test_a_return_is_ignored(self):
		doc_events.on_invoice_change(self._invoice("SN-LISTED", is_return=1))
		self.assertEqual(self.enqueued, [])

	def test_the_same_gun_twice_on_one_invoice_queues_one_job(self):
		"""`job_id` + deduplicate: one gun arrives here several times for one
		event — the invoice hook, then a stock ledger entry per line."""
		doc_events.on_invoice_change(self._invoice("SN-LISTED", "SN-LISTED"))
		job_ids = {job["job_id"] for job in self.enqueued}
		self.assertEqual(job_ids, {"gb-end:SN-LISTED"})
		self.assertTrue(all(job["deduplicate"] for job in self.enqueued))


class WebOrders(_EventCase):
	def _order(self, rows):
		return frappe._dict({"serials": [frappe._dict(r) for r in rows]})

	def test_a_named_gun_reaches_the_listing(self):
		doc_events.on_woo_order_change(
			self._order([{"confirmed_serial": "SN-LISTED"}]))
		self.assertEqual(self._serials_enqueued(), ["SN-LISTED"])

	def test_a_placeholder_gun_reaches_the_listing_too(self):
		"""THE reason this handler reads both columns.

		The placeholder picker excludes reserved guns and NOT listed ones — being
		on GunBroker is not being reserved — so a claim lands on a listed gun
		routinely. Reading only `confirmed_serial` would leave that gun on sale in
		two places, and the fallback is an hour of hourly sweep.
		"""
		doc_events.on_woo_order_change(
			self._order([{"stock_hold_serial": "SN-LISTED"}]))
		self.assertEqual(self._serials_enqueued(), ["SN-LISTED"])

	def test_a_row_naming_the_same_gun_in_both_columns_queues_it_once(self):
		doc_events.on_woo_order_change(self._order([
			{"confirmed_serial": "SN-LISTED", "stock_hold_serial": "SN-LISTED"}]))
		self.assertEqual(self._serials_enqueued(), ["SN-LISTED"])

	def test_an_order_touching_no_listed_gun_costs_nothing(self):
		doc_events.on_woo_order_change(self._order([
			{"confirmed_serial": "SN-OTHER"}, {"stock_hold_serial": ""}]))
		self.assertEqual(self.enqueued, [])


class SerialEdits(_EventCase):
	def _serial(self, changed: tuple, **fields):
		doc = frappe._dict({"name": "SN-LISTED", "gb_item_id": "16087230"})
		doc.update(fields)
		doc.has_value_changed = lambda field: field in changed
		return doc

	def test_a_price_change_sets_the_dirty_bit_and_queues_a_reprice(self):
		"""The bit, not the value. A job carrying a price captured at trigger time
		would publish whichever of two quick edits won the race; reading it when
		the job runs means the last edit is the one that lands."""
		doc_events.on_serial_update(self._serial(("sell_price",)))
		self.assertEqual(self.written.get("gb_price_dirty"), 1)
		self.assertEqual([j["path"] for j in self.enqueued], [REPRICE_TASK])

	def test_a_listing_price_or_title_of_its_own_changing_also_marks_dirty(self):
		"""Both change what the live listing shows, by the same PUT."""
		for field in ("gb_list_price", "gb_listing_title"):
			self.written.clear()
			doc_events.on_serial_update(self._serial((field,)))
			self.assertEqual(self.written.get("gb_price_dirty"), 1, field)

	def test_the_reprice_job_carries_no_job_id(self):
		"""Frappe drops a duplicate enqueue while the first job is RUNNING. With a
		job id, the second of two quick edits would be dropped — leaving the
		listing on the first price with nothing marked dirty, which no sweep would
		notice. Idempotence comes from the dirty bit instead."""
		doc_events.on_serial_update(self._serial(("sell_price",)))
		job = self.enqueued[0]
		self.assertNotIn("job_id", job)
		self.assertNotIn("deduplicate", job)

	def test_a_status_change_re_decides_the_listing(self):
		doc_events.on_serial_update(self._serial(("status",)))
		self.assertEqual(self._serials_enqueued(), ["SN-LISTED"])

	def test_a_promotional_price_change_does_nothing(self):
		"""This phase does not mirror the storefront's sale windows onto
		GunBroker — a marketplace fee makes a sale price a bad trade."""
		doc_events.on_serial_update(self._serial(("sale_sell_price",)))
		self.assertEqual(self.enqueued, [])
		self.assertEqual(self.written, {})

	def test_an_unlisted_gun_is_left_alone_whatever_changes(self):
		doc = self._serial(("sell_price", "status"), gb_item_id="")
		doc_events.on_serial_update(doc)
		self.assertEqual(self.enqueued, [])


class TheEchoGuard(FrappeTestCase):
	def test_gunbroker_has_its_own_inbound_flag(self):
		"""Sharing WooCommerce's flag would mean an order poll from GunBroker also
		silenced every WooCommerce push — and the two channels reacting to each
		other is the entire point of this work."""
		with patch.dict(frappe.flags, {"gb_sync_inbound": True}, clear=False):
			self.assertTrue(doc_events._skip())
		with patch.dict(frappe.flags, {"woo_sync_inbound": True}, clear=False), \
				patch("ffl_integrations.integration_gate.gunbroker_enabled",
					return_value=True):
			self.assertFalse(doc_events._skip())

	def test_switched_off_means_no_handler_does_anything(self):
		with patch("ffl_integrations.integration_gate.gunbroker_enabled",
				return_value=False):
			self.assertTrue(doc_events._skip())


class TheSyncTaskNeverRelists(FrappeTestCase):
	"""The one-way door, tested where it is decided rather than argued about.

	Listing a firearm on a marketplace has a fee and a contract behind it. "The
	gun came back, so we put it up for sale again" is not a decision a queue job
	gets to make.
	"""

	def test_a_gun_that_became_sellable_again_is_not_relisted(self):
		with patch("frappe.db.get_value", return_value="16087230"), \
				patch.object(tasks, "serial_shelf_state",
					return_value={"sellable": True, "reason": None, "detail": {}}), \
				patch.object(tasks.listings, "end_listing") as end:
			tasks.gb_sync_serial_listing_task("SN-1")
		self.assertFalse(end.called)

	def test_a_gun_that_is_no_longer_sellable_has_its_listing_ended(self):
		with patch("frappe.db.get_value", return_value="16087230"), \
				patch.object(tasks, "serial_shelf_state", return_value={
					"sellable": False, "reason": "channel_reserved",
					"detail": {"channel": "woo", "ref": "WOO-ORD-1"}}), \
				patch.object(tasks.listings, "end_listing") as end:
			tasks.gb_sync_serial_listing_task("SN-1")
		self.assertTrue(end.called)
		self.assertIn("woo", end.call_args.kwargs["reason"])

	def test_a_gun_with_no_listing_is_not_even_asked_about(self):
		with patch("frappe.db.get_value", return_value=None), \
				patch.object(tasks, "serial_shelf_state") as shelf:
			tasks.gb_sync_serial_listing_task("SN-1")
		self.assertFalse(shelf.called)


class ACorrectedSerialNumber(_EventCase):
	"""GunBroker cannot be told a listing's serial number has changed.

	The field is accepted by an update and then silently ignored, so a corrected
	gun goes on advertising the old number for as long as the listing stays up —
	a firearm published under a serial that is not the one on the receiver. The
	listing is therefore ENDED and a person re-lists it, with both numbers
	recorded where somebody can read them.
	"""

	def test_a_listed_gun_has_its_listing_ended_with_both_numbers_recorded(self):
		doc_events.on_serial_renamed("SN-OLD", "SN-LISTED")
		self.assertIn("serial changed", self.written["gb_sync_error"])
		self.assertIn("SN-OLD", self.written["gb_sync_error"])
		self.assertIn("SN-LISTED", self.written["gb_sync_error"])
		self.assertEqual(
			[j["path"] for j in self.enqueued],
			["ffl_integrations.gunbroker.tasks.gb_end_listing_task"])
		self.assertIn("SN-OLD", self.enqueued[0]["reason"])

	def test_a_gun_with_no_listing_costs_nothing(self):
		doc_events.on_serial_renamed("SN-OLD", "SN-OTHER")
		self.assertEqual(self.enqueued, [])
		self.assertEqual(self.written, {})

	def test_the_error_write_does_not_bump_modified(self):
		"""A background job that bumps `modified` hands a TimestampMismatch to
		whoever has that Serial No open — which, right after a correction, is
		very likely the person who just made it."""
		captured = {}
		self._patch("frappe.db.set_value",
			side_effect=lambda _dt, _n, f, v=None, **kw: captured.update(kw))
		doc_events.on_serial_renamed("SN-OLD", "SN-LISTED")
		self.assertIs(captured.get("update_modified"), False)

	def test_gunbroker_switched_off_is_silent(self):
		self._patch_object(doc_events, "_skip", return_value=True)
		doc_events.on_serial_renamed("SN-OLD", "SN-LISTED")
		self.assertEqual(self.enqueued, [])


class TheCorrectionCallerGuardsOnTheColumn(FrappeTestCase):
	"""ffl_core enqueues this by dotted path, so it must not assume the column.

	During a deploy the app code is live before `bench migrate` has added the
	GunBroker fields, and a serial correction in that window must not 500 — it
	is a compliance action with a FastBound leg already committed.
	"""

	def test_no_column_means_no_enqueue(self):
		import inspect

		from ffl_core.api import serial_correction

		source = inspect.getsource(serial_correction.correct_serial) \
			if hasattr(serial_correction, "correct_serial") else \
			inspect.getsource(serial_correction)
		self.assertIn('has_column("Serial No", "gb_item_id")', source,
			"the GunBroker enqueue must be gated on the column existing")
		self.assertIn("gunbroker.doc_events.on_serial_renamed", source)
