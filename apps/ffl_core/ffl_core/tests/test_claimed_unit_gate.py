"""A counter ticket may not sell the unit a sold order is already owed.

`web_hold.refuse_held_units` guards ACCESSORIES (no serial → count against the
free shelf). A uniform-price GUN carries a serial, so that gate skips it by
design — and the counter's other gun gate (`_serial_status`, scan-to-add) only
runs when somebody scans. A ticket built any other way (the POS register's own
serial picker, a resumed draft) reached `before_submit` with nothing asking
whether the gun it sells is the last one an online order is waiting on.

`refuse_claimed_firearm_units` is that missing question, and it asks the same
one the scan gate does — `reservations.channel_claim_block_reason` — so the two
cannot disagree about what "owed" means.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.compliance import web_hold

GATE = "ffl_core.compliance.web_hold.refuse_claimed_firearm_units"


def _doc(*rows, name="POS-INV-1", **flags):
	return frappe._dict(
		doctype="POS Invoice", name=name,
		items=[frappe._dict(r) for r in rows], **flags)


class TheGateIsWired(FrappeTestCase):
	def test_it_runs_on_pos_invoice_before_submit_after_the_accessory_gate(self):
		from ffl_core import hooks

		order = hooks.doc_events["POS Invoice"]["before_submit"]
		self.assertIn(GATE, order)
		self.assertLess(
			order.index("ffl_core.compliance.web_hold.refuse_held_units"),
			order.index(GATE))
		self.assertTrue(callable(frappe.get_attr(GATE)))


class TheGate(FrappeTestCase):
	def _run(self, doc, reason=None):
		with patch.object(web_hold, "is_uniform_firearm_item", return_value=True), \
				patch.object(web_hold, "channel_claim_block_reason",
					return_value=reason) as asked:
			web_hold.refuse_claimed_firearm_units(doc)
		return asked

	def test_a_gun_the_pool_cannot_spare_is_refused_with_the_reason(self):
		doc = _doc({"item_code": "MOSIN", "serial_no": "A1"})
		with patch.object(web_hold, "is_uniform_firearm_item", return_value=True), \
				patch.object(web_hold, "channel_claim_block_reason",
					return_value="2 sold order(s) are already owed a MOSIN (web: 2)"):
			with self.assertRaises(frappe.ValidationError) as caught:
				web_hold.refuse_claimed_firearm_units(doc)
		self.assertIn("web: 2", str(caught.exception))

	def test_a_spare_gun_passes(self):
		self._run(_doc({"item_code": "MOSIN", "serial_no": "A1"}))

	def test_the_ticket_asks_with_its_own_name_so_its_own_hold_is_not_a_rival(self):
		asked = self._run(_doc({"item_code": "MOSIN", "serial_no": "A1"}, name="POS-9"))
		asked.assert_called_once_with("MOSIN", selling=1, ignore_hold_ref="POS-9")

	def test_two_guns_of_one_item_are_asked_about_together(self):
		asked = self._run(_doc(
			{"item_code": "MOSIN", "serial_no": "A1"},
			{"item_code": "MOSIN", "serial_no": "A2"}))
		asked.assert_called_once_with("MOSIN", selling=2, ignore_hold_ref="POS-INV-1")

	def test_lines_without_a_serial_are_not_this_gates_business(self):
		"""Accessories are `refuse_held_units`'s."""
		asked = self._run(_doc({"item_code": "AMMO", "qty": 5}))
		asked.assert_not_called()

	def test_a_serialised_line_of_anything_but_a_uniform_gun_is_not_asked_about(self):
		doc = _doc({"item_code": "SCOPE", "serial_no": "Z1"})
		with patch.object(web_hold, "is_uniform_firearm_item", return_value=False), \
				patch.object(web_hold, "channel_claim_block_reason") as asked:
			web_hold.refuse_claimed_firearm_units(doc)
		asked.assert_not_called()

	def test_every_refused_item_is_named_in_one_message(self):
		doc = _doc({"item_code": "MOSIN", "serial_no": "A1"},
			{"item_code": "SKS", "serial_no": "B1"})
		with patch.object(web_hold, "is_uniform_firearm_item", return_value=True), \
				patch.object(web_hold, "channel_claim_block_reason",
					side_effect=lambda code, **_kw: f"{code} is owed"):
			with self.assertRaises(frappe.ValidationError) as caught:
				web_hold.refuse_claimed_firearm_units(doc)
		message = str(caught.exception)
		self.assertIn("MOSIN is owed", message)
		self.assertIn("SKS is owed", message)

	def test_returns_consolidated_copies_and_web_orders_own_invoices_are_exempt(self):
		for flag in ({"is_return": 1}, {"is_consolidated": 1},
				{"woo_online_order": "WOO-1"}):
			with self.subTest(flag=flag):
				asked = self._run(
					_doc({"item_code": "MOSIN", "serial_no": "A1"}, **flag),
					reason="refuse")
				asked.assert_not_called()
