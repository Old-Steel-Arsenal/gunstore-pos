"""Sales Invoice ``on_submit`` guard.

A submitted invoice carrying drop-ship lines but NO live Distributor Order is a
silent hole: the customer has paid for goods nobody ordered. Two ways in that
create_order's in-transaction hook does not cover —

* an **amend** (the amended invoice submits on its own; create_order never runs);
* a **hand-built desktop invoice** (staff adds a drop-ship item on the SI form).

Recoverable context (destination FFL from the SI; accessory ship-to from the SI's
shipping Address) → rebuild the Draft DO and alert. Unrecoverable → refuse the
submit and point at Create Order. NEVER auto-place.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ffl_integrations.distributor import counter

_dict = counter.frappe._dict


def _si(*, lines, ffl=None, address=None, flags=None, name="SINV-9"):
	return types.SimpleNamespace(
		name=name, customer="Ann", ffl_destination_ffl=ffl,
		shipping_address_name=address,
		flags=_dict(**(flags or {})),
		items=[_dict(**ln) for ln in lines])


_ACC = {"item_code": "RSR-ACC", "delivered_by_supplier": 1,
	"dropship_distributor": "RSR", "qty": 1, "rate": 40}
_GUN = {"item_code": "RSR-GUN", "delivered_by_supplier": 1,
	"dropship_distributor": "RSR", "qty": 1, "rate": 500}
_LOCAL = {"item_code": "LOCAL", "delivered_by_supplier": 0, "qty": 1, "rate": 20}
_TO_STORE = {"item_code": "TS-GUN", "delivered_by_supplier": 0,
	"ffl_to_store_distributor": "RSR", "qty": 1, "rate": 600}

_FULL_ADDRESS = {"address_line1": "5 Elm", "address_line2": "",
	"city": "Springfield", "state": "TX", "pincode": "75024"}


class SubmitGuardTest(unittest.TestCase):
	def _run(self, doc, *, existing=(), address_row=None, is_firearm=()):
		rebuilt, alerts = [], []

		def _get_all(dt, *a, **k):
			if dt != "Distributor Order":
				return []
			return [_dict(**n) if isinstance(n, dict) else _dict(name=n) for n in existing]

		def _get_value(dt, name=None, field=None, *a, **k):
			if dt == "Address":
				return _dict(**address_row) if address_row else None
			if dt == "Item" and field == "is_firearm":
				return 1 if name in is_firearm else 0
			return None

		ps = [
			mock.patch.object(counter.frappe, "get_all", _get_all),
			mock.patch.object(counter.frappe.db, "get_value", _get_value),
			mock.patch.object(counter, "ffl_counter_order_distributor",
				lambda si, ctx: rebuilt.append((si, ctx))),
			mock.patch.object(counter, "notify_admins",
				lambda title, body, **k: alerts.append(title), create=True),
		]
		for p in ps:
			p.start()
			# Register each stop separately: unittest runs cleanups LIFO, which
			# unwinds overlapping patches correctly. Stopping them FIFO in one
			# cleanup restores a STUB as the 'original' when two patchers target
			# the same attribute — that leaked frappe.get_doc process-wide.
			self.addCleanup(p.stop)
		counter.guard_sales_invoice_submit(doc, method="on_submit")
		return rebuilt, alerts

	# ---- the cases that must do NOTHING -----------------------------------

	def test_create_order_path_is_skipped(self):
		# create_order fires the hook itself right after submit; the guard must not
		# race it into a duplicate order.
		doc = _si(lines=[_ACC], flags={"ffl_counter_distributor_hook_pending": True})
		rebuilt, _ = self._run(doc)
		self.assertEqual(rebuilt, [])

	def test_invoice_without_dropship_lines_is_a_noop(self):
		rebuilt, _ = self._run(_si(lines=[_LOCAL]))
		self.assertEqual(rebuilt, [])

	def test_existing_distributor_order_is_a_noop(self):
		rebuilt, _ = self._run(_si(lines=[_ACC], address="ADDR-1"),
			existing=["DO-1"], address_row=_FULL_ADDRESS)
		self.assertEqual(rebuilt, [])

	# ---- rebuild ----------------------------------------------------------

	def test_amended_accessory_invoice_rebuilds_and_alerts(self):
		doc = _si(lines=[_ACC], address="ADDR-1")
		rebuilt, alerts = self._run(doc, address_row=_FULL_ADDRESS)
		self.assertEqual(len(rebuilt), 1)
		si, ctx = rebuilt[0]
		self.assertEqual(si, "SINV-9")
		self.assertEqual(ctx["ship_to"]["state"], "TX")
		self.assertEqual(len(ctx["lines"]), 1)
		self.assertTrue(alerts, "a silently-rebuilt order must be announced")

	def test_firearm_invoice_rebuilds_from_the_invoice_ffl(self):
		doc = _si(lines=[_GUN], ffl="ATF-REC-1")
		rebuilt, _ = self._run(doc, is_firearm=("RSR-GUN",))
		self.assertEqual(len(rebuilt), 1)
		self.assertEqual(rebuilt[0][1]["destination_ffl"], "ATF-REC-1")

	# ---- procure to store --------------------------------------------------

	def _to_store_lines(self, rebuilt):
		return [(ln["item_code"], ln["distributor"]) for ln in rebuilt[0][1]["lines"]
			if ln.get("lane") == "to_store"]

	def test_to_store_row_rebuilds_without_a_destination(self):
		rebuilt, alerts = self._run(_si(lines=[_TO_STORE]))
		self.assertEqual(self._to_store_lines(rebuilt), [("TS-GUN", "RSR")])
		self.assertTrue(alerts)

	def test_a_live_drop_ship_order_does_not_hide_a_missing_to_store_order(self):
		# cancel withdrew the Draft to-store order; the Complete drop-ship order was
		# carried forward to the amendment
		rebuilt, _ = self._run(_si(lines=[_ACC, _TO_STORE], address="ADDR-1"),
			existing=[{"name": "DO-1", "order_type": "Dropship Accessory",
				"distributor": "RSR"}], address_row=_FULL_ADDRESS)
		self.assertEqual(len(rebuilt), 1)
		self.assertEqual([ln["item_code"] for ln in rebuilt[0][1]["lines"]],
			["TS-GUN"])

	def test_to_store_is_checked_per_house(self):
		ss_row = {**_TO_STORE, "item_code": "SS-GUN", "ffl_to_store_distributor": "Sports South"}
		rebuilt, _ = self._run(_si(lines=[_TO_STORE, ss_row]),
			existing=[{"name": "DO-2", "order_type": "Procure To Store", "distributor": "RSR"}])
		self.assertEqual(self._to_store_lines(rebuilt), [("SS-GUN", "Sports South")])

	def test_to_store_house_with_a_live_order_is_a_noop(self):
		rebuilt, _ = self._run(_si(lines=[_TO_STORE]),
			existing=[{"name": "DO-2", "order_type": "Procure To Store", "distributor": "RSR"}])
		self.assertEqual(rebuilt, [])

	# ---- refuse -----------------------------------------------------------

	def test_firearm_without_a_destination_ffl_refuses_the_submit(self):
		doc = _si(lines=[_GUN], ffl=None)
		with self.assertRaises(counter.frappe.ValidationError):
			self._run(doc, is_firearm=("RSR-GUN",))

	def test_accessory_without_a_recoverable_ship_to_refuses_the_submit(self):
		doc = _si(lines=[_ACC], address=None)
		with self.assertRaises(counter.frappe.ValidationError):
			self._run(doc)

	def test_accessory_with_a_partial_address_refuses_the_submit(self):
		doc = _si(lines=[_ACC], address="ADDR-1")
		with self.assertRaises(counter.frappe.ValidationError):
			self._run(doc, address_row={"address_line1": "5 Elm", "city": "",
				"state": "TX", "pincode": "75024"})


if __name__ == "__main__":
	unittest.main()
