"""Create Order's Pickup in store rule, read off the shipped JS.

The page must gate Pickup on the same thing the server does — a regulated
firearm FROM STOCK, not any firearm. An antique needs no 4473 and no destination
FFL, and a regulated gun ordered to the store is handed over on its own POS
Invoice when it arrives (api/counter_pickup) — so neither may lock the toggle,
and a pickup with such a gun needs no destination FFL. This repo ships no JS
test runner, so the rule is asserted on the source.
"""
from __future__ import annotations

import unittest

from ffl_core.tests.front_end_invariants import block, code_only, read


def _page() -> str:
	return read("ffl_core", "ffl_core", "page", "create_order", "create_order.js")


class PickupGateTest(unittest.TestCase):
	def test_the_pickup_toggle_reads_a_gun_from_stock_not_any_firearm(self):
		src = code_only(_page())
		for opener in ("\t_onPickupChange() {", "\t_syncDestSection() {"):
			with self.subTest(method=opener.strip()):
				body = code_only(block(_page(), opener, "\t"))
				self.assertIn("this._hasStockRegulated()", body)
				self.assertNotIn("this._isPickup() && this._hasFirearm()", body)
				self.assertNotIn("this._hasRegulated() && this._isPickup()", body)
		# and the box itself is never disabled by an antique or an ordered gun
		for pred in ("this._hasFirearm()", "this._hasRegulated()"):
			self.assertNotIn(f"pickup.$input?.prop('disabled', {pred})", src)
			self.assertNotIn(f"pickup?.$input?.prop('disabled', {pred})", src)

	def test_a_gun_from_stock_is_a_regulated_one_with_a_serial(self):
		self.assertIn("_hasRegulated() { return this.lines.some((l) => l.is_firearm && !l.is_antique); }",
			_page())
		self.assertIn("_hasStockRegulated() { return this.lines.some((l) => l.is_firearm && !l.is_antique && !!l.serial); }",
			_page())

	def test_a_pickup_needs_no_destination_ffl(self):
		body = code_only(block(_page(), "\t_reflectReadiness() {", "\t"))
		self.assertIn("this._hasRegulated() && !this.destinationFfl && !this._isPickup()", body)
		dest = code_only(block(_page(), "\t_syncDestSection() {", "\t"))
		self.assertIn("this._hasFirearm() && !this._isPickup()", dest)


if __name__ == "__main__":
	unittest.main()
