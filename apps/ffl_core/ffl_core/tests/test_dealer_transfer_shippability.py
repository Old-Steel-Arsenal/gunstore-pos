"""dealer_transfer.py's shared, non-throwing
shippability gates.

Before this fix, consignment_out._dealer_shippable (the Consignment Out
dealer-picker badge) and require_shippable_ffl_dealer's customer-path branch
(the settlement SI's submit-time gate) each independently re-implemented the
SAME three-condition check (Customer.ffl_number / ez_check_status /
ffl_expiry_date) — a real duplication risk — and the badge's docstring
falsely claimed it mirrored "the exact same gate" ship_consignment_out
enforces at submit. That gate is actually manual_order._require_valid_ffl
(ATF FFL Record-based: expiry + a complete address), a DIFFERENT condition
set the old badge never checked at all, so it could disagree with the real
gate in both directions:

  - Customer.ez_check_status stale/never re-verified, but the linked
    atf_ffl_record is perfectly valid (unexpired + complete address) -> old
    badge: red (blocks Submit) even though ship_consignment_out would
    actually succeed.
  - Customer.ez_check_status freshly "Verified" and Customer.ffl_expiry_date
    current, but the linked atf_ffl_record is missing its address -> old
    badge: green, submit throws later at ship_consignment_out's
    _require_valid_ffl.

dealer_transfer.customer_ffl_shippable / atf_record_shippable are the two
extracted, non-throwing, shared checks; consignment_out._dealer_shippable
now prefers the ATF-record-based truth when a record is linked (matching
_resolve_ship_destination's own resolution order), falling back to the
Customer-level check only when none is linked yet.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate


class DealerTransferShippabilityFixtures(FrappeTestCase):
	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")

	def _dealer(self, suffix: str, *, ez_check_status="Verified", ffl_expiry_days=365,
			atf_ffl: str | None = None) -> str:
		name = f"_Test M1 Shippability Dealer {suffix}"
		values = {
			"customer_name": name, "customer_group": "Commercial",
			"territory": "All Territories", "is_ffl_dealer": 1,
			"ffl_number": f"1-99-888-01-2D-0020{suffix}",
			"ez_check_status": ez_check_status,
			"ffl_expiry_date": add_days(nowdate(), ffl_expiry_days),
			"atf_ffl_record": atf_ffl,
		}
		if frappe.db.exists("Customer", name):
			frappe.db.set_value("Customer", name, values, update_modified=False)
		else:
			frappe.get_doc({"doctype": "Customer", **values}).insert(ignore_permissions=True)
		return name

	def _atf(self, suffix: str, *, has_address=True, expiry_days=365) -> str:
		ffl_number = f"1-99-888-01-2D-0021{suffix}"
		values = {
			"licensee_name": f"Test M1 ATF {suffix} LLC",
			"street": "1 Test Way" if has_address else "",
			"city": "Austin" if has_address else "",
			"state": "TX" if has_address else "",
			"zip": "78701" if has_address else "",
			"expiry_date": add_days(nowdate(), expiry_days),
		}
		if frappe.db.exists("ATF FFL Record", ffl_number):
			frappe.db.set_value("ATF FFL Record", ffl_number, values, update_modified=False)
		else:
			frappe.get_doc({
				"doctype": "ATF FFL Record", "ffl_number": ffl_number, **values,
			}).insert(ignore_permissions=True)
		return ffl_number


class CustomerFflShippableUnit(DealerTransferShippabilityFixtures):
	"""The extracted check backing require_shippable_ffl_dealer's customer-
	path branch (no explicit destination FFL on the document) — same three
	conditions, same order, as a plain unit test on the dict shape
	list_consignment_dealers/require_shippable_ffl_dealer both build."""

	def test_blank_ffl_number_is_not_shippable(self):
		from ffl_core.compliance.dealer_transfer import customer_ffl_shippable

		ok, reason = customer_ffl_shippable({
			"ffl_number": "", "ez_check_status": "Verified",
			"ffl_expiry_date": add_days(nowdate(), 30)})
		self.assertFalse(ok)
		self.assertTrue(reason)

	def test_unverified_is_not_shippable(self):
		from ffl_core.compliance.dealer_transfer import customer_ffl_shippable

		ok, _reason = customer_ffl_shippable({
			"ffl_number": "1-1", "ez_check_status": "Pending",
			"ffl_expiry_date": add_days(nowdate(), 30)})
		self.assertFalse(ok)

	def test_expired_is_not_shippable(self):
		from ffl_core.compliance.dealer_transfer import customer_ffl_shippable

		ok, _reason = customer_ffl_shippable({
			"ffl_number": "1-1", "ez_check_status": "Verified",
			"ffl_expiry_date": add_days(nowdate(), -1)})
		self.assertFalse(ok)

	def test_verified_unexpired_is_shippable(self):
		from ffl_core.compliance.dealer_transfer import customer_ffl_shippable

		ok, reason = customer_ffl_shippable({
			"ffl_number": "1-1", "ez_check_status": "Verified",
			"ffl_expiry_date": add_days(nowdate(), 30)})
		self.assertTrue(ok)
		self.assertIsNone(reason)


class AtfRecordShippableUnit(DealerTransferShippabilityFixtures):
	"""The extracted check mirroring manual_order._require_valid_ffl's exact
	condition set — the REAL hard gate ship_consignment_out enforces."""

	def test_blank_record_is_not_shippable(self):
		from ffl_core.compliance.dealer_transfer import atf_record_shippable

		ok, _reason = atf_record_shippable("")
		self.assertFalse(ok)

	def test_missing_address_is_not_shippable_by_default(self):
		from ffl_core.compliance.dealer_transfer import atf_record_shippable

		ffl = self._atf("1", has_address=False)
		ok, reason = atf_record_shippable(ffl)
		self.assertFalse(ok)
		self.assertTrue(reason)

	def test_missing_address_is_shippable_when_address_not_required(self):
		"""require_shippable_ffl_dealer's dest-set branch calls this with
		require_address=False — a lighter re-check than create-time (upstream
		callers already ran the full address-complete check before ever
		building the document; see that function's docstring)."""
		from ffl_core.compliance.dealer_transfer import atf_record_shippable

		ffl = self._atf("2", has_address=False)
		ok, _reason = atf_record_shippable(ffl, require_address=False)
		self.assertTrue(ok)

	def test_complete_record_is_shippable(self):
		from ffl_core.compliance.dealer_transfer import atf_record_shippable

		ffl = self._atf("3")
		ok, reason = atf_record_shippable(ffl)
		self.assertTrue(ok)
		self.assertIsNone(reason)

	def test_expired_record_is_not_shippable(self):
		from ffl_core.compliance.dealer_transfer import atf_record_shippable

		ffl = self._atf("4", expiry_days=-1)
		ok, _reason = atf_record_shippable(ffl)
		self.assertFalse(ok)


class RequireValidFflIsAThinWrapper(DealerTransferShippabilityFixtures):
	"""manual_order._require_valid_ffl was a
	hand-copied THIRD statement of atf_record_shippable's condition set, free
	to drift from the badge and the submit hook that are supposed to agree
	with it. It is now a throwing wrapper over dealer_transfer.atf_record_block,
	keeping only the operator wording and the three dialog titles.

	Behaviour must be unchanged — that is what the rest of the suite (build
	order, dispose, ship_consignment_out) proves. These tests pin the two
	things a refactor could quietly lose: the reason-code -> title mapping,
	and the fact that there is no second condition set left behind."""

	def test_delegates_to_atf_record_block(self):
		from unittest.mock import patch

		from ffl_core.api.manual_order import _require_valid_ffl

		# atf_record_block says "shippable" -> the wrapper must throw nothing,
		# and must not consult the database on its own to second-guess it.
		with patch(
			"ffl_core.compliance.dealer_transfer.atf_record_block", return_value=None
		) as block_mock:
			_require_valid_ffl("1-99-888-01-2D-99999")  # a record that does not exist
		block_mock.assert_called_once()
		self.assertTrue(block_mock.call_args.kwargs.get("require_address"))

	def _throw_of(self, ffl: str) -> dict:
		from ffl_core.api.manual_order import _require_valid_ffl

		# clear_messages(), not `frappe.message_log = []`: assigning the module
		# attribute replaces frappe's request-local proxy with a plain list for
		# the rest of the process, and every later test's msgprint then lands
		# where get_message_log() never looks.
		frappe.clear_messages()
		with self.assertRaises(frappe.ValidationError):
			_require_valid_ffl(ffl)
		return frappe.get_message_log()[-1]

	def test_unknown_record_throws_not_on_file_with_no_title_of_its_own(self):
		entry = self._throw_of("1-99-888-01-2D-00000")
		self.assertIn("is not on file", entry["message"])
		# frappe.throw() with no title stamps its generic default — i.e. this
		# path deliberately has no dialog title of its own, as before.
		self.assertEqual(entry["title"], "Message")

	def test_expired_record_keeps_its_own_title(self):
		entry = self._throw_of(self._atf("E", expiry_days=-1))
		self.assertEqual(entry["title"], "Destination FFL Expired")
		self.assertIn("unexpired", entry["message"])

	def test_addressless_record_keeps_its_own_title(self):
		entry = self._throw_of(self._atf("A", has_address=False))
		self.assertEqual(entry["title"], "Destination FFL Address Incomplete")
		self.assertIn("complete address", entry["message"])

	def test_a_good_record_passes(self):
		from ffl_core.api.manual_order import _require_valid_ffl

		ffl = self._atf("G")
		_require_valid_ffl(ffl)  # must not throw
