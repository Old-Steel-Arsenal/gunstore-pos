"""C&R (Type 03) verification — the SERVER-SIDE final defense.

The dispose dialog's field gating is a convenience. It is bypassable by anyone
who can call `frappe.call` — which is every logged-in staff member — so every
rule the dialog enforces is re-enforced here, and these tests call the server
functions directly with no dialog involved. That is the whole point of the
file: if it only tested the happy path it would be testing the dialog's
politeness, not the gate.

What must NOT be mocked here (and is not): the decision itself. `frappe.throw`
is redirected so the *title* can be asserted (titles are part of contract D0c
and drive which dialog the POS page shows), but which check fires, on what
input, is real code running over real values. `_TitledThrow` still aborts the
function exactly like the real throw, so "did it refuse?" is genuinely proved.
One test deliberately uses the unpatched frappe.throw to prove the real
ValidationError path works end to end.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import getdate

# The implementation module, not ffl_woo_sync's re-export shim: these tests
# patch module-level names (`nowdate`, `now_datetime`, `frappe.throw`), and a
# re-exported name is a second reference to the same object — patching the alias
# would rebind only the alias while the real call keeps the original. The Woo
# behaviour under test is unchanged; only the module's home moved (GunBroker is
# a second C&R-shipping channel, so the rule lives in ffl_core).
from ffl_core.compliance import cr_transfer

CR = "1-23-456-03-7C-89012"
DEALER = "5-99-888-01-9D-26128"


class _TitledThrow(Exception):
	"""Stand-in for frappe.throw that keeps the title for assertions."""

	def __init__(self, msg="", title=None):
		super().__init__(msg)
		self.msg = str(msg)
		self.title = str(title) if title else ""


def _throw(msg="", title=None, **kwargs):
	raise _TitledThrow(msg, title)


def _order(**over):
	o = MagicMock()
	o.name = "WOO-ORD-00042"
	o.is_cr_transfer = 1
	o.ffl_license = CR
	o.atf_ffl_record = ""
	o.customer_name = "Jane Q. Collector"
	o.customer_phone = "512-555-0100"
	o.ship_to_state = "TX"
	o.ship_to_zip = "78701"
	o.cr_verified_by = ""
	o.cr_verified_at = None
	for k, v in over.items():
		setattr(o, k, v)
	return o


def _validated_details(**over):
	"""The payload shape _persist_cr_details actually receives in production:
	the output of _validate_cr_details, whose license_expiry is a parsed date.
	Feeding it the raw string shape would be testing a call that never happens."""
	d = _details(**over)
	if isinstance(d["license_expiry"], str) and d["license_expiry"]:
		d["license_expiry"] = getdate(d["license_expiry"])
	return d


def _details(**over):
	d = {
		"license_number": CR,
		"license_expiry": "2028-07-31",
		"contact_name": "Jane Q. Collector",
		"premise_street": "123 Main St",
		"premise_street2": "Apt 4",
		"premise_city": "Austin",
		"premise_state": "TX",
		"premise_zip": "78701",
	}
	d.update(over)
	return d


class ValidateCrDetailsTest(FrappeTestCase):
	"""Every branch of the gate, called directly — no dialog in the picture."""

	def setUp(self):
		p = patch.object(cr_transfer.frappe, "throw", side_effect=_throw)
		p.start()
		self.addCleanup(p.stop)
		np = patch.object(cr_transfer, "nowdate", return_value="2026-07-29")
		np.start()
		self.addCleanup(np.stop)

	def test_accepts_a_complete_payload(self):
		clean = cr_transfer._validate_cr_details(_details(), _order())
		self.assertEqual(clean["license_number"], CR)
		self.assertEqual(clean["premise_city"], "Austin")

	def test_values_are_stripped(self):
		clean = cr_transfer._validate_cr_details(
			_details(contact_name="  Jane Q. Collector  ", premise_city=" Austin "), _order())
		self.assertEqual(clean["contact_name"], "Jane Q. Collector")
		self.assertEqual(clean["premise_city"], "Austin")

	def test_missing_payload_is_refused(self):
		"""D0c①: the bypass case — dispose called with no cr_details at all."""
		for empty in (None, "", {}):
			with self.subTest(payload=empty):
				with self.assertRaises(_TitledThrow) as cm:
					cr_transfer._validate_cr_details(empty, _order())
				self.assertEqual(cm.exception.title, "C&R Verification Required")

	def test_missing_required_field_names_it(self):
		"""D0c②: a partial payload must say WHICH field, not just 'invalid'."""
		with self.assertRaises(_TitledThrow) as cm:
			cr_transfer._validate_cr_details(_details(premise_city=""), _order())
		self.assertIn("City", cm.exception.msg)

	def test_missing_several_fields_lists_them_all(self):
		with self.assertRaises(_TitledThrow) as cm:
			cr_transfer._validate_cr_details(
				_details(premise_city="", premise_zip="", contact_name=""), _order())
		for label in ("City", "ZIP", "name"):
			self.assertIn(label, cm.exception.msg)

	def test_address_2_is_optional(self):
		clean = cr_transfer._validate_cr_details(_details(premise_street2=""), _order())
		self.assertEqual(clean["premise_street2"], "")

	def test_non_cr_license_is_refused(self):
		"""D0c③: a Type 01 number must not ride the collector path."""
		with self.assertRaises(_TitledThrow) as cm:
			cr_transfer._validate_cr_details(_details(license_number=DEALER), _order())
		self.assertIn("03", cm.exception.msg)

	def test_license_with_trailing_newline_cannot_reach_storage(self):
		"""Server side of a cross-repo finding.

		The danger was never "a newline exists" — it was a value passing
		validation and then being PERSISTED in a form we never validated (and
		reaching FastBound normalized to 15 chars, so the stored licence and the
		checked licence differ). The payload is stripped before validation, so
		the clean number is both what is checked and what is written. The regex
		itself still refuses the raw string — see test_ffl_number — which is what
		protects the paths that do not strip."""
		clean = cr_transfer._validate_cr_details(
			_details(license_number=CR + "\n"), _order())
		self.assertEqual(clean["license_number"], CR)
		self.assertNotIn("\n", clean["license_number"])

	def test_license_with_interior_junk_is_still_refused(self):
		"""Stripping is only whitespace at the ends — it must not become a
		licence-number laundry."""
		with self.assertRaises(_TitledThrow):
			cr_transfer._validate_cr_details(
				_details(license_number="1-23-456-03-7C-89\n012"), _order())

	def test_expired_license_is_refused(self):
		"""D0c④ — yesterday is expired; the title drives the POS dialog."""
		with self.assertRaises(_TitledThrow) as cm:
			cr_transfer._validate_cr_details(_details(license_expiry="2026-07-28"), _order())
		self.assertEqual(cm.exception.title, "C&R License Expired")

	def test_licence_expiring_today_is_still_valid(self):
		"""A licence is valid THROUGH its expiration date — an off-by-one here
		turns a lawful transfer away (or lets an expired one through)."""
		clean = cr_transfer._validate_cr_details(_details(license_expiry="2026-07-29"), _order())
		self.assertEqual(clean["license_expiry"], date(2026, 7, 29))

	def test_unparseable_expiry_is_refused(self):
		with self.assertRaises(_TitledThrow):
			cr_transfer._validate_cr_details(_details(license_expiry="next year"), _order())

	def test_unresolvable_state_is_refused(self):
		with self.assertRaises(_TitledThrow) as cm:
			cr_transfer._validate_cr_details(_details(premise_state="XX"), _order())
		self.assertIn("US state", cm.exception.msg)

	def test_full_state_name_is_accepted_and_normalized(self):
		clean = cr_transfer._validate_cr_details(_details(premise_state="Texas"), _order())
		self.assertEqual(clean["premise_state"], "TX")

	def test_state_mismatch_with_shipping_address_is_refused(self):
		"""The parcel ships to the order address; if the licensed premises
		is in another STATE, shipping it is a substantive violation, not a typo."""
		with self.assertRaises(_TitledThrow) as cm:
			cr_transfer._validate_cr_details(_details(premise_state="OK"), _order(ship_to_state="TX"))
		self.assertIn("OK", cm.exception.msg)

	def test_zip_mismatch_with_shipping_address_is_refused(self):
		with self.assertRaises(_TitledThrow):
			cr_transfer._validate_cr_details(_details(premise_zip="73301"), _order(ship_to_zip="78701"))

	def test_zip_plus_four_matches_its_five_digit_form(self):
		"""78701-1234 vs 78701 is the same place — blocking it would train staff
		to retype the address until the gate shuts up."""
		clean = cr_transfer._validate_cr_details(
			_details(premise_zip="78701-1234"), _order(ship_to_zip="78701"))
		self.assertEqual(clean["premise_zip"], "78701-1234")

	def test_street_difference_is_allowed(self):
		"""Deliberately NOT blocked: 'St' vs 'Street' must not stop a
		lawful shipment, or staff learn to make the fields agree by fiction."""
		cr_transfer._validate_cr_details(
			_details(premise_street="123 Main Street"), _order())

	def test_missing_order_side_address_does_not_falsely_block(self):
		"""An order with no captured ship-to can't contradict anything."""
		cr_transfer._validate_cr_details(_details(), _order(ship_to_state="", ship_to_zip=""))

	def test_json_string_payload_is_accepted(self):
		"""frappe.call sends nested objects as a JSON string."""
		import json
		clean = cr_transfer._validate_cr_details(json.dumps(_details()), _order())
		self.assertEqual(clean["license_number"], CR)


class RealThrowIntegrationTest(FrappeTestCase):
	"""One case with the REAL frappe.throw, so the patched tests above can't be
	hiding a wiring break in the actual raise path."""

	def test_real_frappe_throw_raises_validation_error(self):
		with patch.object(cr_transfer, "nowdate", return_value="2026-07-29"):
			with self.assertRaises(frappe.ValidationError):
				cr_transfer._validate_cr_details(None, _order())


class NonCrOrderTest(FrappeTestCase):
	def test_cr_details_on_a_non_cr_order_is_refused(self):
		"""D0c⑤: guards against a dialog/RPC mix-up quietly stamping collector
		verification onto an ordinary dealer transfer."""
		with patch.object(cr_transfer.frappe, "throw", side_effect=_throw):
			with self.assertRaises(_TitledThrow) as cm:
				cr_transfer.reject_cr_details_on_non_cr_order(_details(), _order(is_cr_transfer=0))
		self.assertIn("not a C&R transfer", cm.exception.msg)

	def test_no_details_on_a_non_cr_order_is_fine(self):
		with patch.object(cr_transfer.frappe, "throw", side_effect=_throw):
			cr_transfer.reject_cr_details_on_non_cr_order(None, _order(is_cr_transfer=0))


class _RecStub:
	"""Minimal ATF FFL Record stand-in: unset attributes simply don't exist."""

	def __init__(self, name):
		self.name = name
		self.flags = SimpleNamespace(ignore_permissions=False)
		self.saved = 0

	def save(self, *a, **kw):
		self.saved += 1


class EnsureCrFflRecordTest(FrappeTestCase):
	"""The ATF FFL Record is written from the values a human just confirmed —
	never silently from ship_to (that would downgrade 'verified' to 'guessed')."""

	def _call(self, exists, **over):
		kwargs = dict(ffl_number=CR, licensee_name="Jane Q. Collector",
			street="123 Main St", street2="Apt 4", city="Austin", state="TX",
			zip_code="78701", expiry_date="2028-07-31", phone="512-555-0100")
		kwargs.update(over)
		# A plain stub, NOT MagicMock: every unset attribute on a MagicMock is a
		# truthy auto-created child, so `assertFalse(doc.live_verified_at)` would
		# pass against a mock no matter what the code does. With a stub, "never
		# assigned" is observably absent.
		doc = _RecStub(CR)
		# Delegate every OTHER doctype to the real frappe.get_doc. A blanket
		# return_value also answers frappe's own internal lookups — notably
		# get_doc("System Settings"), which now_datetime() reaches through for the
		# timezone — and hands them this stub. That only shows up when the
		# timezone cache is cold, i.e. it passes in a full suite run and fails
		# when the module runs alone: a test whose result depends on cache order.
		real_get_doc = frappe.get_doc

		def get_doc_for(*args, **kwargs):
			if args and args[0] == "ATF FFL Record":
				return doc
			return real_get_doc(*args, **kwargs)

		with patch("frappe.db.exists", return_value=exists), \
			 patch("frappe.get_doc", side_effect=get_doc_for) as get_doc, \
			 patch("frappe.new_doc", return_value=doc) as new_doc:
			name = cr_transfer.ensure_cr_ffl_record(**kwargs)
		return name, doc, get_doc, new_doc

	def test_creates_the_record_with_type_03_provenance(self):
		name, doc, _get, new_doc = self._call(exists=False)
		self.assertEqual(name, CR)
		self.assertTrue(new_doc.called)
		self.assertEqual(doc.license_type, "03 - Collector of Curios and Relics")
		self.assertEqual(doc.loaded_from, "cr_web_order")
		self.assertEqual(doc.licensee_name, "Jane Q. Collector")
		self.assertEqual(doc.street, "123 Main St")
		self.assertEqual(doc.city, "Austin")
		self.assertEqual(doc.state, "TX")
		self.assertEqual(doc.zip, "78701")
		self.assertEqual(doc.ffl_number, CR)

	def test_live_verified_at_stays_empty(self):
		"""This is a human reading a licence copy, NOT an ATF online check —
		stamping live_verified_at would forge a verification we never did."""
		_name, doc, _get, _new = self._call(exists=False)
		self.assertIsNone(getattr(doc, "live_verified_at", None))

	def test_existing_record_is_updated_with_this_verification(self):
		name, doc, get_doc, new_doc = self._call(exists=True, city="Dallas")
		self.assertEqual(name, CR)
		self.assertTrue(get_doc.called)
		self.assertFalse(new_doc.called)
		self.assertEqual(doc.city, "Dallas")
		self.assertEqual(doc.saved, 1)


class PersistCrDetailsTest(FrappeTestCase):
	def test_writes_all_nine_fields(self):
		order = _order()
		with patch.object(cr_transfer, "now_datetime", return_value="2026-07-29 10:00:00"), \
			 patch.dict(cr_transfer.frappe.session, {"user": "clerk@example.com"}):
			cr_transfer._persist_cr_details(order, _validated_details())
		self.assertEqual(order.cr_contact_name, "Jane Q. Collector")
		self.assertEqual(order.cr_license_expiry, date(2028, 7, 31))
		self.assertEqual(order.cr_premise_street, "123 Main St")
		self.assertEqual(order.cr_premise_street2, "Apt 4")
		self.assertEqual(order.cr_premise_city, "Austin")
		self.assertEqual(order.cr_premise_state, "TX")
		self.assertEqual(order.cr_premise_zip, "78701")
		self.assertEqual(order.cr_verified_by, "clerk@example.com")
		self.assertEqual(order.cr_verified_at, "2026-07-29 10:00:00")

	def test_corrected_licence_number_clears_the_cached_atf_record(self):
		"""The load-bearing line. _resolve_and_validate_ffl short-circuits on
		atf_ffl_record and never looks at ffl_license again — so a corrected
		number with a stale cached record ships the gun to the PREVIOUS FFL."""
		order = _order(ffl_license="1-23-456-03-7C-00000", atf_ffl_record="1-23-456-03-7C-00000")
		with patch.object(cr_transfer, "now_datetime", return_value="2026-07-29 10:00:00"):
			cr_transfer._persist_cr_details(order, _details())
		self.assertEqual(order.ffl_license, CR)
		self.assertEqual(order.atf_ffl_record, "")

	def test_unchanged_licence_keeps_the_cached_record(self):
		order = _order(ffl_license=CR, atf_ffl_record=CR)
		with patch.object(cr_transfer, "now_datetime", return_value="2026-07-29 10:00:00"):
			cr_transfer._persist_cr_details(order, _details())
		self.assertEqual(order.atf_ffl_record, CR)

	def test_reverification_with_identical_values_keeps_the_first_verifier(self):
		"""Retry after an unrelated dispose failure must not rewrite the audit
		stamp — the original attestation stands."""
		order = _order(cr_verified_by="first@example.com", cr_verified_at="2026-07-29 09:00:00")
		order.cr_contact_name = "Jane Q. Collector"
		# A DATE OBJECT, as a Date column really reads back — not the string the
		# payload carries. With a string here the comparison would trivially match
		# and this test would pass against code that re-stamps on every retry.
		order.cr_license_expiry = date(2028, 7, 31)
		order.cr_premise_street = "123 Main St"
		order.cr_premise_street2 = "Apt 4"
		order.cr_premise_city = "Austin"
		order.cr_premise_state = "TX"
		order.cr_premise_zip = "78701"
		with patch.object(cr_transfer, "now_datetime", return_value="2026-07-29 10:00:00"), \
			 patch.dict(cr_transfer.frappe.session, {"user": "second@example.com"}):
			cr_transfer._persist_cr_details(order, _details())
		self.assertEqual(order.cr_verified_by, "first@example.com")
		self.assertEqual(order.cr_verified_at, "2026-07-29 09:00:00")

	def test_changed_values_restamp_the_verifier(self):
		"""Whoever attested to the values now stored is the accountable party."""
		order = _order(cr_verified_by="first@example.com", cr_verified_at="2026-07-29 09:00:00")
		order.cr_premise_city = "Austin"
		with patch.object(cr_transfer, "now_datetime", return_value="2026-07-29 10:00:00"), \
			 patch.dict(cr_transfer.frappe.session, {"user": "second@example.com"}):
			cr_transfer._persist_cr_details(order, _details(premise_city="Dallas"))
		self.assertEqual(order.cr_verified_by, "second@example.com")
		self.assertEqual(order.cr_premise_city, "Dallas")


class ClearCrVerificationTest(FrappeTestCase):
	def test_clears_every_verification_field(self):
		"""When the licence number changes, the previous verification is about a
		DIFFERENT licence — leaving it would push the old holder's name and
		address to FastBound under the new number."""
		order = _order()
		order.cr_contact_name = "Jane Q. Collector"
		order.cr_license_expiry = "2028-07-31"
		order.cr_premise_street = "123 Main St"
		order.cr_premise_street2 = "Apt 4"
		order.cr_premise_city = "Austin"
		order.cr_premise_state = "TX"
		order.cr_premise_zip = "78701"
		order.cr_verified_by = "clerk@example.com"
		order.cr_verified_at = "2026-07-29 10:00:00"
		cr_transfer._clear_cr_verification(order)
		for f in cr_transfer.CR_VERIFICATION_FIELDNAMES:
			with self.subTest(field=f):
				self.assertFalse(getattr(order, f), f"{f} survived the clear")


class ExpiryTypeRoundTripTest(FrappeTestCase):
	"""The retry comparison must survive the Date column's type round-trip.

	`cr_license_expiry` is a Date, so a saved order reads it back as
	`datetime.date` while the dialog always submits a string. Comparing those
	directly never matches, so every dispose retry would re-stamp
	cr_verified_by/at — silently recording whoever clicked Dispose last as the
	person who verified the licence, on the one channel where the staff check is
	the entire paper trail.
	"""

	def test_stored_date_object_vs_submitted_string_is_not_a_change(self):
		order = _order(cr_verified_by="first@example.com",
			cr_verified_at="2026-07-29 09:00:00")
		order.cr_contact_name = "Jane Q. Collector"
		order.cr_license_expiry = date(2028, 7, 31)      # as the DB returns it
		order.cr_premise_street = "123 Main St"
		order.cr_premise_street2 = "Apt 4"
		order.cr_premise_city = "Austin"
		order.cr_premise_state = "TX"
		order.cr_premise_zip = "78701"
		with patch.object(cr_transfer, "now_datetime", return_value="2026-07-29 10:00:00"), \
			 patch.dict(cr_transfer.frappe.session, {"user": "second@example.com"}):
			cr_transfer._persist_cr_details(order, _details())   # payload has the STRING
		self.assertEqual(order.cr_verified_by, "first@example.com")
		self.assertEqual(order.cr_verified_at, "2026-07-29 09:00:00")

	def test_a_genuinely_different_expiry_still_counts_as_a_change(self):
		"""The control — the normalisation must not swallow a real edit."""
		order = _order(cr_verified_by="first@example.com",
			cr_verified_at="2026-07-29 09:00:00")
		order.cr_contact_name = "Jane Q. Collector"
		order.cr_license_expiry = date(2027, 1, 1)
		order.cr_premise_street = "123 Main St"
		order.cr_premise_street2 = "Apt 4"
		order.cr_premise_city = "Austin"
		order.cr_premise_state = "TX"
		order.cr_premise_zip = "78701"
		with patch.object(cr_transfer, "now_datetime", return_value="2026-07-29 10:00:00"), \
			 patch.dict(cr_transfer.frappe.session, {"user": "second@example.com"}):
			cr_transfer._persist_cr_details(order, _details())
		self.assertEqual(order.cr_verified_by, "second@example.com")


class RejectOnNonCrSensitivityTest(FrappeTestCase):
	"""parse_cr_details always returns all eight keys, so "is there a payload?"
	must ask whether any VALUE is set — otherwise an empty object throws while
	unparseable junk slips through, which is exactly backwards."""

	def test_empty_object_is_not_treated_as_a_payload(self):
		with patch.object(cr_transfer.frappe, "throw", side_effect=_throw):
			cr_transfer.reject_cr_details_on_non_cr_order("{}", _order(is_cr_transfer=0))
			cr_transfer.reject_cr_details_on_non_cr_order({}, _order(is_cr_transfer=0))

	def test_unparseable_junk_is_not_treated_as_a_payload(self):
		with patch.object(cr_transfer.frappe, "throw", side_effect=_throw):
			cr_transfer.reject_cr_details_on_non_cr_order("not json", _order(is_cr_transfer=0))

	def test_a_real_payload_is_still_refused(self):
		with patch.object(cr_transfer.frappe, "throw", side_effect=_throw):
			with self.assertRaises(_TitledThrow):
				cr_transfer.reject_cr_details_on_non_cr_order(_details(), _order(is_cr_transfer=0))
