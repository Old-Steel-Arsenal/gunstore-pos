"""Create Order → a C&R (Type 03) collector as the destination FFL.

A counter order to a collector cannot use Verify/Lookup: the ATF's public FFL
list holds no Type 03 licensees, so eZ Check has nothing to return and the
number alone carries no address or expiry — while the ship-to, the
destination-based sales tax and the FFL Transfer disposition all read those
off an `ATF FFL Record`. `save_cr_ffl` is the counter's way in: a clerk reads
the license copy, the server validates it with the SAME rules the web queue's
dispose uses (compliance.cr_transfer), and an ordinary directory record comes
out.

The load-bearing assertion in here is the last one in the happy path: the
record this endpoint writes passes `_require_valid_ffl`, the same shipping
gate every other destination must clear. Without it these tests would only
prove that a row was written, not that an order could ship to it.
"""
from __future__ import annotations

import itertools
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from ffl_core.api import manual_order
from ffl_core.compliance.cr_transfer import CR_DETAIL_KEYS
from ffl_core.tests.front_end_invariants import block, code_only, read

DEALER = "5-99-888-01-9D-26128"

# A licence number private to each test. The refusal tests prove "nothing was
# written" by looking the number up, and a shared constant makes that assertion
# read the SITE instead of the call: any collector already on file under it —
# an earlier manual run, a real C&R customer — turns the check green-or-red for
# reasons the test never caused. The sequence keeps the 03 type group (that is
# the part under test) and varies only the serial group.
_serial = itertools.count(89012)


def _cr() -> str:
	return f"1-23-456-03-7C-{next(_serial):05d}"


def _details(cr: str, **over) -> dict:
	payload = {
		"license_number": cr,
		"license_expiry": add_days(nowdate(), 400),
		"contact_name": "Jane Q. Collector",
		"premise_street": "123 Main St",
		"premise_street2": "",
		"premise_city": "Austin",
		"premise_state": "TX",
		"premise_zip": "78701",
	}
	payload.update(over)
	return payload


class CounterCrDestinationTest(FrappeTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.cr = _cr()

	def test_a_collector_becomes_a_shippable_destination_ffl(self):
		name = manual_order.save_cr_ffl(_details(self.cr))["name"]
		self.assertEqual(name, self.cr)
		rec = frappe.get_doc("ATF FFL Record", name)
		self.assertEqual(rec.license_type, "03 - Collector of Curios and Relics")
		self.assertEqual(rec.licensee_name, "Jane Q. Collector")
		self.assertEqual(rec.city, "Austin")
		self.assertEqual(rec.state, "TX")
		# Provenance names the channel whose operator read the licence — the web
		# queue's dispose writes cr_web_order for the same act.
		self.assertEqual(rec.loaded_from, "cr_counter_order")
		# A human reading a licence copy is not an ATF online check.
		self.assertFalse(rec.get("live_verified_at"))
		# The point of the whole endpoint: an order may now ship here.
		manual_order._require_valid_ffl(name)

	def test_the_payload_may_arrive_as_json(self):
		"""frappe.call serializes the nested object — the browser's shape must
		work, not only the Python caller's."""
		name = manual_order.save_cr_ffl(frappe.as_json(_details(self.cr)))["name"]
		self.assertEqual(name, self.cr)

	def test_a_second_reading_updates_the_same_record(self):
		manual_order.save_cr_ffl(_details(self.cr))
		manual_order.save_cr_ffl(_details(self.cr, premise_city="Dallas",
			contact_name="Jane Q. Collector Jr"))
		rec = frappe.get_doc("ATF FFL Record", self.cr)
		self.assertEqual(rec.city, "Dallas")
		self.assertEqual(rec.licensee_name, "Jane Q. Collector Jr")

	def test_a_dealer_license_is_refused(self):
		"""The box says C&R; a Type 01 through it would put a dealer in the
		directory with no verification behind it."""
		with self.assertRaises(frappe.ValidationError):
			manual_order.save_cr_ffl(_details(self.cr, license_number=DEALER))
		self.assertFalse(frappe.db.exists("ATF FFL Record", DEALER))

	def test_an_expired_license_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			manual_order.save_cr_ffl(_details(self.cr, license_expiry=add_days(nowdate(), -1)))
		self.assertFalse(frappe.db.exists("ATF FFL Record", self.cr))

	def test_a_missing_field_is_refused_before_anything_is_written(self):
		with self.assertRaises(frappe.ValidationError):
			manual_order.save_cr_ffl(_details(self.cr, premise_city=""))
		self.assertFalse(frappe.db.exists("ATF FFL Record", self.cr))

	def test_a_bad_state_is_refused(self):
		"""The state drives the in-state/out-of-state tax decision, so garbage
		in it is not a cosmetic problem."""
		with self.assertRaises(frappe.ValidationError):
			manual_order.save_cr_ffl(_details(self.cr, premise_state="Texas?"))


class CreateOrderCrFrontEndTest(unittest.TestCase):
	"""This repo ships no JS test runner, so the page's half of the contract is
	asserted on the shipped source."""

	def _page(self) -> str:
		return read("ffl_core", "ffl_core", "page", "create_order", "create_order.js")

	def test_the_page_posts_every_key_the_payload_is_frozen_to(self):
		body = code_only(block(self._page(), "\t_crDetails() {", "\t"))
		for key in CR_DETAIL_KEYS:
			self.assertIn(f"{key}:", body)

	def test_the_c_and_r_block_replaces_the_directory_lookup(self):
		"""Verify/Lookup cannot resolve a Type 03 — leaving them on screen in
		C&R mode offers the clerk a button that can only fail."""
		body = code_only(block(self._page(), "\t_syncCrSection() {", "\t"))
		self.assertIn("'#dest-standard').toggle(!cr)", body)
		self.assertIn("'#dest-cr').toggle(cr)", body)

	def test_switching_modes_drops_the_destination_chosen_in_the_other_one(self):
		body = code_only(block(self._page(), "\t_onCrToggle() {", "\t"))
		self.assertIn("this.fields.dest_ffl.set_value('')", body)

	def test_a_typed_but_unsaved_license_is_not_a_destination(self):
		"""Readiness must key on the saved record, never on the filled form."""
		body = code_only(block(self._page(), "\t_reflectReadiness() {", "\t"))
		self.assertIn("!this.destinationFfl", body)
		self.assertIn("this._isCr()", body)


if __name__ == "__main__":
	unittest.main()
