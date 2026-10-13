"""An amend must not sever the drop-ship cost chain.

Partial refunds are done as **cancel + amend** (SOP). That
leaves SI-1 cancelled while DO-A — the order holding the ack cost and its cost PI —
still points at it. Two real consequences:

* the Sales Report resolves the amended invoice's drop-ship line to NO cost
  (`cost=0`, `missing_cost=True`), so the report-vs-PI tie-out breaks after
  EVERY amend — and amend is the *routine* action, not an edge case;
* the on_submit rebuild guard sees no order for SI-1-1 and creates a phantom Draft
  one, which if confirmed is a SECOND real purchase of goods already bought.

Carrying the LIVE orders forward onto the amended invoice fixes both at once, which
is why it beats teaching the report to walk `amended_from` (that would leave the
phantom order in place).
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ffl_integrations.distributor import counter, order_state

_dict = counter.frappe._dict


def _si(name, *, amended_from=None, lines=()):
	return types.SimpleNamespace(
		name=name, amended_from=amended_from, customer="Ann",
		ffl_destination_ffl=None, shipping_address_name=None,
		flags=_dict(), items=[_dict(**ln) for ln in lines])


class CarryForwardTest(unittest.TestCase):
	def _run(self, doc, *, orders_by_source, chain=None, get_doc=None):
		"""orders_by_source: {invoice: [{name,status}]}; chain: {invoice: amended_from}"""
		moved = []

		def _get_all(dt, *a, **k):
			filters = (k.get("filters") or (a[0] if a else {})) or {}
			src = filters.get("source_name")
			rows = orders_by_source.get(src, [])
			# honour a status != Cancelled filter when the caller passes one
			if filters.get("status"):
				rows = [r for r in rows if r["status"] != order_state.CANCELLED]
			return [_dict(**r) for r in rows]

		def _get_value(dt, name=None, field=None, *a, **k):
			if dt == "Sales Invoice" and field == "amended_from":
				return (chain or {}).get(name)
			return None

		def _set_value(dt, name=None, field=None, value=None, *a, **k):
			moved.append((name, field, value))

		def _default_get_doc(dt, name=None, *a, **k):
			# carry_forward's audit-comment step calls get_doc("Distributor
			# Order").add_comment. Unmocked, that loads the real controller, and since
			# _get_value stubs get_value("DocType", ...) to None the module resolves to
			# Core -> ModuleNotFoundError. A stubbed frappe never loads a controller,
			# so this only bites on a real bench. A case that needs to SEE the comment
			# passes its own get_doc; everyone else gets this harmless stub.
			d = types.SimpleNamespace(name=name)
			d.add_comment = lambda *a, **k: None
			return d

		ps = [
			mock.patch.object(counter.frappe, "get_all", _get_all),
			mock.patch.object(counter.frappe, "get_doc", get_doc or _default_get_doc),
			mock.patch.object(counter.frappe.db, "get_value", _get_value),
			mock.patch.object(counter.frappe.db, "set_value", _set_value),
		]
		for p in ps:
			p.start()
			self.addCleanup(p.stop)
		count = counter.carry_forward_amended_orders(doc)
		return count, moved

	def test_live_order_is_repointed_to_the_amended_invoice(self):
		doc = _si("SI-1-1", amended_from="SI-1")
		count, moved = self._run(doc, orders_by_source={
			"SI-1": [{"name": "DO-A", "status": order_state.COMPLETE}]})
		self.assertEqual(count, 1)
		self.assertEqual(moved, [("DO-A", "source_name", "SI-1-1")])

	def test_an_edit_moves_the_order_to_the_invoice_that_replaced_it(self):
		"""Editing a counter order is cancel + rebuild, not an amend: the hook hands
		the live orders to the new invoice by name, along the same one re-point."""
		moved = []

		def _get_all(dt, *a, **k):
			filters = (k.get("filters") or (a[0] if a else {})) or {}
			rows = [] if filters.get("source_name") != "SI-OLD" else [
				_dict(name="DO-A", status=order_state.PLACED)]
			return rows

		def _get_doc(dt, name=None, *a, **k):
			d = types.SimpleNamespace(name=name)
			d.add_comment = lambda *a, **k: None
			return d

		for p in [
			mock.patch.object(counter.frappe, "get_all", _get_all),
			mock.patch.object(counter.frappe, "get_doc", _get_doc),
			mock.patch.object(counter.frappe.db, "set_value",
				lambda dt, name=None, field=None, value=None, *a, **k: moved.append(
					(name, field, value))),
		]:
			p.start()
			self.addCleanup(p.stop)
		self.assertEqual(counter.carry_forward_replaced_orders("SI-OLD", "SI-NEW"), 1)
		self.assertEqual(moved, [("DO-A", "source_name", "SI-NEW")])

	def test_the_move_leaves_an_audit_comment(self):
		"""set_value writes no Version, and this link is what an accountant reconciles
		along (DO → PI → SI). The move must leave a readable trace."""
		comments = []

		def _get_doc(dt, name=None, *a, **k):
			d = types.SimpleNamespace(name=name)
			d.add_comment = lambda kind, text: comments.append((name, kind, text))
			return d

		# Pass get_doc INTO _run rather than patching separately: _run patches get_doc
		# too, and two patchers on the same attribute race — the second-registered
		# wins and the comment collector never sees the call.
		self._run(_si("SI-1-1", amended_from="SI-1"), get_doc=_get_doc,
			orders_by_source={
				"SI-1": [{"name": "DO-A", "status": order_state.COMPLETE}]})
		self.assertEqual(len(comments), 1)
		name, kind, text = comments[0]
		self.assertEqual((name, kind), ("DO-A", "Comment"))
		self.assertIn("SI-1", text)
		self.assertIn("SI-1-1", text)

	def test_cancelled_orders_stay_with_the_cancelled_invoice(self):
		doc = _si("SI-1-1", amended_from="SI-1")
		count, moved = self._run(doc, orders_by_source={
			"SI-1": [{"name": "DO-X", "status": order_state.CANCELLED}]})
		self.assertEqual((count, moved), (0, []))

	def test_a_chain_of_amends_is_walked_to_the_root(self):
		# SI-1 -> SI-1-1 -> SI-1-2: the order still hangs off the ORIGINAL.
		doc = _si("SI-1-2", amended_from="SI-1-1")
		count, moved = self._run(
			doc,
			orders_by_source={"SI-1": [{"name": "DO-A", "status": order_state.COMPLETE}]},
			chain={"SI-1-1": "SI-1"})
		self.assertEqual(count, 1)
		self.assertEqual(moved, [("DO-A", "source_name", "SI-1-2")])

	def test_invoice_that_is_not_an_amend_is_a_noop(self):
		count, moved = self._run(_si("SI-9"), orders_by_source={})
		self.assertEqual((count, moved), (0, []))

	def test_a_cycle_cannot_hang_the_walk(self):
		doc = _si("SI-B", amended_from="SI-A")
		count, _ = self._run(doc, orders_by_source={}, chain={"SI-A": "SI-B"})
		self.assertEqual(count, 0)


class RebuildNoLongerPhantomsTest(unittest.TestCase):
	"""The secondary win: with the order carried forward, the rebuild guard finds
	it and creates nothing — no phantom Draft order that could be confirmed into a
	second real purchase."""

	def test_amend_with_a_carried_order_does_not_rebuild(self):
		rebuilt = []
		doc = _si("SI-1-1", amended_from="SI-1", lines=[
			{"item_code": "RSR-ACC", "delivered_by_supplier": 1,
				"dropship_distributor": "RSR", "qty": 1, "rate": 40}])

		# After carry-forward the order is on SI-1-1, so the existing-order probe hits.
		ps = [
			mock.patch.object(counter, "carry_forward_amended_orders", lambda d: 1),
			mock.patch.object(counter.frappe, "get_all",
				lambda *a, **k: [_dict(name="DO-A")]),
			mock.patch.object(counter, "ffl_counter_order_distributor",
				lambda si, ctx: rebuilt.append(si)),
		]
		for p in ps:
			p.start()
			self.addCleanup(p.stop)
		counter.guard_sales_invoice_submit(doc, method="on_submit")
		self.assertEqual(rebuilt, [])

	def test_amend_that_removes_the_dropship_line_still_carries_the_order(self):
		"""A partial refund that drops the drop-ship line entirely still
		leaves a shipped, invoiced order behind. Returning early on 'no drop-ship
		lines' would pin it to the CANCELLED invoice and break the tie-out from the
		other side, so carry-forward must run BEFORE that check."""
		carried = []
		doc = _si("SI-1-1", amended_from="SI-1", lines=[
			{"item_code": "LOCAL", "delivered_by_supplier": 0, "qty": 1, "rate": 10}])
		ps = [
			mock.patch.object(counter, "carry_forward_amended_orders",
				lambda d: carried.append(d.name) or 0),
			mock.patch.object(counter.frappe, "get_all", lambda *a, **k: []),
			mock.patch.object(counter, "ffl_counter_order_distributor", lambda si, ctx: None),
		]
		for p in ps:
			p.start()
			self.addCleanup(p.stop)
		counter.guard_sales_invoice_submit(doc, method="on_submit")
		self.assertEqual(carried, ["SI-1-1"])

	def test_carry_forward_runs_before_the_existing_order_probe(self):
		order = []
		doc = _si("SI-1-1", amended_from="SI-1", lines=[
			{"item_code": "RSR-ACC", "delivered_by_supplier": 1,
				"dropship_distributor": "RSR", "qty": 1, "rate": 40}])
		ps = [
			mock.patch.object(counter, "carry_forward_amended_orders",
				lambda d: order.append("carry") or 0),
			mock.patch.object(counter.frappe, "get_all",
				lambda *a, **k: order.append("probe") or [_dict(name="DO-A")]),
			mock.patch.object(counter, "ffl_counter_order_distributor", lambda si, ctx: None),
		]
		for p in ps:
			p.start()
			self.addCleanup(p.stop)
		counter.guard_sales_invoice_submit(doc, method="on_submit")
		self.assertEqual(order[:2], ["carry", "probe"])


class MixedLegRebuildRefusedTest(unittest.TestCase):
	"""A rebuild that would have to place BOTH
	an FDS firearm leg and a direct accessory leg is refused, because `_recover_ship_to`
	can only offer ONE address and for a firearm order that Address is the FFL premise:
	the accessory would ship to the transfer dealer instead of the buyer."""

	def _rebuild(self, lines, *, guns=()):
		doc = _si("SI-7", lines=lines)
		doc.shipping_address_name = "ADDR-1"
		doc.ffl_destination_ffl = "ATF-1"

		def _get_value(dt, name=None, field=None, *a, **k):
			if dt == "Item" and field == "is_firearm":
				return 1 if name in guns else 0
			if dt == "Address":
				return _dict(address_line1="5 Elm", city="Springfield", state="TX",
					pincode="75024", address_line2="")
			return None

		ps = [
			mock.patch.object(counter, "carry_forward_amended_orders", lambda d: 0),
			mock.patch.object(counter.frappe, "get_all", lambda *a, **k: []),
			mock.patch.object(counter.frappe.db, "get_value", _get_value),
			mock.patch.object(counter, "ffl_counter_order_distributor", lambda si, ctx: None),
			mock.patch.object(counter, "notify_admins", lambda *a, **k: None),
		]
		for p in ps:
			p.start()
			self.addCleanup(p.stop)
		counter.guard_sales_invoice_submit(doc, method="on_submit")

	_ACC = {"item_code": "RSR-ACC", "delivered_by_supplier": 1,
		"dropship_distributor": "RSR", "qty": 1, "rate": 40}
	_GUN = {"item_code": "RSR-GUN", "delivered_by_supplier": 1,
		"dropship_distributor": "RSR", "qty": 1, "rate": 500}

	def test_mixed_legs_refuse_the_rebuild(self):
		with self.assertRaises(counter.frappe.ValidationError):
			self._rebuild([self._ACC, self._GUN], guns=("RSR-GUN",))

	def test_accessory_only_rebuild_still_works(self):
		self._rebuild([self._ACC])          # no raise

	def test_firearm_only_rebuild_still_works(self):
		self._rebuild([self._GUN], guns=("RSR-GUN",))


if __name__ == "__main__":
	unittest.main()
