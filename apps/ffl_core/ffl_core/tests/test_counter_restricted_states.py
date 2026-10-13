"""P4b slice 4 — drop-ship restricted-state validation.

Some SKUs cannot ship to some states. The destination that matters differs by
line: a drop-ship ACCESSORY ships to the buyer's own address, so its restriction
is checked against ``ship_to.state``; a drop-ship FIREARM ships to the transfer
FFL (FDS), so its restriction is checked against the FFL PREMISE state. Enforced
server-side at create_order — a hard block, not merely the panel's advisory badge.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ffl_core.api import manual_order


class StateRestrictedHelperTest(unittest.TestCase):
	def test_csv_membership_case_insensitive(self):
		self.assertTrue(manual_order._state_restricted("NY,NJ,CA", "ny"))
		self.assertTrue(manual_order._state_restricted(" ny , nj ", "NJ"))
		self.assertFalse(manual_order._state_restricted("NY,NJ", "TX"))

	def test_list_form_and_empty(self):
		self.assertTrue(manual_order._state_restricted(["NY", "NJ"], "NJ"))
		self.assertFalse(manual_order._state_restricted("", "NY"))
		self.assertFalse(manual_order._state_restricted("NY", ""))     # unknown state
		self.assertFalse(manual_order._state_restricted(None, "NY"))

	# ---- spelling must not defeat a restriction ------------------

	def test_spelled_out_state_matches_a_two_letter_restriction(self):
		"""THE hole: an operator types "Texas"; the item is restricted in "TX". A raw
		compare misses and the restricted goods ship."""
		self.assertTrue(manual_order._state_restricted("TX,NY", "Texas"))
		self.assertTrue(manual_order._state_restricted("TX", "TEXAS"))
		self.assertTrue(manual_order._state_restricted("TX", " texas "))

	def test_two_letter_state_matches_a_spelled_out_restriction(self):
		# …and the mirror: the restriction list itself may be spelled out.
		self.assertTrue(manual_order._state_restricted("Texas,New York", "TX"))
		self.assertTrue(manual_order._state_restricted(["New Jersey"], "NJ"))

	def test_unrelated_states_still_do_not_match(self):
		self.assertFalse(manual_order._state_restricted("Texas", "CA"))
		self.assertFalse(manual_order._state_restricted("TX", "California"))

	def test_unknown_spellings_fall_back_to_exact_compare(self):
		# normalize_us_state returns None for a non-US value; don't lose the match.
		self.assertTrue(manual_order._state_restricted("Ontario", "ontario"))
		self.assertFalse(manual_order._state_restricted("Ontario", "Quebec"))


class DropshipRestrictionsTest(unittest.TestCase):
	def setUp(self):
		self.guns = {"RSR-GUN"}
		p = mock.patch.object(manual_order, "is_firearm_item",
			lambda code: code in self.guns)
		p.start()
		self.addCleanup(p.stop)

	def _rows(self, **over):
		base = {"item_code": "RSR-ACC", "source": "dropship",
			"restricted_states": "NY,NJ"}
		base.update(over)
		return [base]

	def test_accessory_blocked_by_ship_to_state(self):
		with self.assertRaises(manual_order.frappe.ValidationError):
			manual_order._validate_dropship_restrictions(
				self._rows(), {"state": "NJ"}, None)

	def test_accessory_ok_for_unrestricted_ship_to(self):
		# No raise.
		manual_order._validate_dropship_restrictions(
			self._rows(), {"state": "TX"}, None)

	def test_accessory_blocked_by_a_spelled_out_ship_to_state(self):
		# "New Jersey" must trip an "NJ" restriction.
		with self.assertRaises(manual_order.frappe.ValidationError):
			manual_order._validate_dropship_restrictions(
				self._rows(), {"state": "New Jersey"}, None)

	def test_firearm_blocked_by_a_spelled_out_ffl_premise_state(self):
		# The FFL premise state is read straight off the ATF record, so it can be
		# spelled out too — the firearm leg needs the same normalisation.
		with mock.patch.object(manual_order, "ffl_premise",
				lambda ffl: types.SimpleNamespace(state="New York")):
			with self.assertRaises(manual_order.frappe.ValidationError):
				manual_order._validate_dropship_restrictions(
					self._rows(item_code="RSR-GUN"), {"state": "TX"}, "FFL-1")

	def test_firearm_checked_against_ffl_premise_not_ship_to(self):
		# The customer address is NJ (would trip an accessory) but the gun ships to
		# the FFL premise in TX — the firearm restriction must read the FFL state.
		with mock.patch.object(manual_order, "ffl_premise",
				lambda ffl: types.SimpleNamespace(state="NY")):
			with self.assertRaises(manual_order.frappe.ValidationError):
				manual_order._validate_dropship_restrictions(
					self._rows(item_code="RSR-GUN"), {"state": "TX"}, "FFL-1")

	def test_firearm_ok_when_ffl_premise_unrestricted(self):
		with mock.patch.object(manual_order, "ffl_premise",
				lambda ffl: types.SimpleNamespace(state="TX")):
			manual_order._validate_dropship_restrictions(
				self._rows(item_code="RSR-GUN"), {"state": "NJ"}, "FFL-1")

	def test_local_rows_are_ignored(self):
		# A local (non-dropship) row carries no restriction check here.
		manual_order._validate_dropship_restrictions(
			[{"item_code": "RSR-ACC", "source": "local"}], {"state": "NJ"}, None)


class DropshipShipToRequiredTest(unittest.TestCase):
	"""A drop-ship ACCESSORY ships to the buyer's door, so the order needs a
	COMPLETE destination up front. Without this the DO is built against a half address
	AND `_state_restricted` silently passes on a blank state: an item restricted in the
	buyer's state would ship because nobody knew the state."""

	def setUp(self):
		p = mock.patch.object(manual_order, "is_firearm_item",
			lambda code: code == "RSR-GUN")
		p.start()
		self.addCleanup(p.stop)

	_ACC_ROW = {"item_code": "RSR-ACC", "source": "dropship"}
	_GUN_ROW = {"item_code": "RSR-GUN", "source": "dropship"}
	_FULL = {"ship_to": {"address_line1": "5 Elm", "city": "Springfield",
		"state": "TX", "pincode": "75024"}}

	def test_complete_ship_to_passes(self):
		manual_order._assert_dropship_ship_to([self._ACC_ROW], self._FULL)

	def test_missing_state_is_refused(self):
		payload = {"ship_to": {"address_line1": "5 Elm", "city": "Springfield",
			"state": "", "pincode": "75024"}}
		with self.assertRaises(manual_order.frappe.ValidationError):
			manual_order._assert_dropship_ship_to([self._ACC_ROW], payload)

	def test_no_ship_to_at_all_is_refused(self):
		with self.assertRaises(manual_order.frappe.ValidationError):
			manual_order._assert_dropship_ship_to([self._ACC_ROW], {})

	def test_missing_zip_is_refused(self):
		payload = {"ship_to": {"address_line1": "5 Elm", "city": "Springfield",
			"state": "TX", "pincode": ""}}
		with self.assertRaises(manual_order.frappe.ValidationError):
			manual_order._assert_dropship_ship_to([self._ACC_ROW], payload)

	def test_firearm_only_order_needs_no_buyer_address(self):
		# An FDS gun ships to the transfer FFL, not the buyer's door.
		manual_order._assert_dropship_ship_to([self._GUN_ROW], {})

	def test_local_accessory_order_is_unaffected(self):
		manual_order._assert_dropship_ship_to(
			[{"item_code": "RSR-ACC", "source": "local"}], {})


class SingleDestinationStateTest(unittest.TestCase):
	"""Every drop-ship leg on ONE counter order must land in
	the same state. The invoice's tax is evaluated against a SINGLE destination, so an
	FDS gun going to a transfer FFL in one state while a direct accessory goes to a
	buyer address in another must tax exactly one leg wrong (XOR-TX). Same-state legs
	are always equivalent — the ordinary case — so this only forces a split when it
	genuinely must."""

	def setUp(self):
		p = mock.patch.object(manual_order, "is_firearm_item",
			lambda code: code == "RSR-GUN")
		p.start()
		self.addCleanup(p.stop)

	_GUN = {"item_code": "RSR-GUN", "source": "dropship"}
	_ACC = {"item_code": "RSR-ACC", "source": "dropship"}

	def _check(self, rows, buyer_state, ffl_state):
		with mock.patch.object(manual_order, "ffl_premise",
				lambda ffl: types.SimpleNamespace(state=ffl_state)):
			manual_order._assert_single_destination_state(
				rows, {"ship_to": {"state": buyer_state}}, "FFL-1")

	def test_same_state_legs_pass(self):
		self._check([self._GUN, self._ACC], buyer_state="TX", ffl_state="TX")

	def test_state_spelling_variants_still_count_as_same(self):
		self._check([self._GUN, self._ACC], buyer_state="Texas", ffl_state="TX")

	def test_cross_state_legs_are_refused(self):
		with self.assertRaises(manual_order.frappe.ValidationError):
			self._check([self._GUN, self._ACC], buyer_state="CA", ffl_state="TX")

	def test_single_leg_orders_are_unaffected(self):
		self._check([self._GUN], buyer_state="CA", ffl_state="TX")   # gun only
		self._check([self._ACC], buyer_state="CA", ffl_state="TX")   # accessory only


if __name__ == "__main__":
	unittest.main()
