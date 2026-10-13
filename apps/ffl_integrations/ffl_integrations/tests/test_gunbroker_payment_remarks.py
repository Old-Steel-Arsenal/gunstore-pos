"""The order number in a refund entry's remarks has to survive being saved.

The GunBroker refund alerts tell the operator to put
"GunBroker Order #<id>" in a hand-keyed Payment Entry's remarks, because
that marker is the ONLY thing tying a manual refund back to its order: the
cash-basis schedule matches on it, and an unmatched refund leaves the order
sitting in gross receipts for the year with the money already gone.

ERPNext throws that text away. `PaymentEntry.set_remarks()` returns early only
when `custom_remarks` is set; otherwise it rebuilds the field from the amount
and party, on every save. The operator types the order number, presses save,
and it is gone — with no error, and a schedule that reads as though no refund
ever happened.

The tick is easy to miss and impossible to notice afterwards, so the system
sets it: a Payment Entry that pays a customer and carries the marker gets
`custom_remarks` turned on, with a message saying so. Not a refusal — the entry
is correct, it is the field that needs protecting.

**Timing is the whole thing.** `set_remarks()` runs inside the controller's
`validate()`, and a hooked `validate` runs AFTER the controller's (the same
ordering `hooks.py` already relies on for the drop-ship re-pin). A guard there
would set the flag one step too late: correct on the next save, useless on the
one that wiped the text. It has to be `before_validate`.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations import hooks
from ffl_integrations.gunbroker import revenue

_MARKER = "Refund of GunBroker Order #1001 — cheque 40012"


def _pe(**kw):
	doc = frappe._dict({
		"doctype": "Payment Entry",
		"payment_type": "Pay",
		"party_type": "Customer",
		"party": "Jane Buyer",
		"custom_remarks": 0,
		"remarks": _MARKER,
	})
	doc.update(kw)
	return doc


class TheMarkerIsProtectedOnTheWayIn(FrappeTestCase):
	def _run(self, doc):
		with patch.object(revenue, "frappe") as fake:
			fake._dict = frappe._dict
			revenue.keep_gunbroker_remarks(doc)
			return fake.msgprint.call_count

	def test_a_refund_to_a_customer_gets_the_tick(self):
		doc = _pe()
		self._run(doc)
		self.assertEqual(doc.custom_remarks, 1)
		self.assertEqual(doc.remarks, _MARKER)

	def test_and_says_so_rather_than_doing_it_silently(self):
		# Silently changing a field the operator can see is how the next person
		# learns the wrong lesson about which ticks matter.
		self.assertEqual(self._run(_pe()), 1)

	def test_an_entry_that_already_has_the_tick_is_left_alone(self):
		# No second message on every subsequent save of the same entry.
		doc = _pe(custom_remarks=1)
		self.assertEqual(self._run(doc), 0)
		self.assertEqual(doc.custom_remarks, 1)

	def test_a_payment_with_no_marker_is_not_touched(self):
		doc = _pe(remarks="Refund for the counter sale")
		self._run(doc)
		self.assertEqual(doc.custom_remarks, 0)

	def test_a_payment_with_no_remarks_at_all_is_not_touched(self):
		doc = _pe(remarks=None)
		self._run(doc)
		self.assertEqual(doc.custom_remarks, 0)

	def test_a_receipt_is_not_touched(self):
		# The receipts this system writes set custom_remarks themselves. Turning
		# it on for an incoming payment would freeze ERPNext's own description
		# on entries nobody typed a marker into.
		doc = _pe(payment_type="Receive")
		self._run(doc)
		self.assertEqual(doc.custom_remarks, 0)

	def test_a_payment_to_a_supplier_is_not_touched(self):
		# Paying GunBroker's fee bill can legitimately mention an order number
		# in its remarks; it is not a refund to a buyer and nothing reads it.
		doc = _pe(party_type="Supplier")
		self._run(doc)
		self.assertEqual(doc.custom_remarks, 0)

	def test_a_string_zero_from_an_import_is_not_a_ticked_box(self):
		# REST, Data Import and bench console hand over whatever was in the
		# payload, and "0" is truthy in Python. Read without cint() the guard
		# concludes the box is already ticked and stands down — on precisely
		# the paths it exists for, since the Desk form cannot produce this.
		doc = _pe(custom_remarks="0")
		self._run(doc)
		self.assertEqual(doc.custom_remarks, 1)

	def test_an_empty_string_is_not_a_ticked_box_either(self):
		doc = _pe(custom_remarks="")
		self._run(doc)
		self.assertEqual(doc.custom_remarks, 1)

	def test_the_marker_it_looks_for_is_the_one_the_schedule_matches_on(self):
		# Two copies of the same string, and the schedule's copy is the one that
		# decides whether a refund is seen at all.
		from ffl_core.marketplace_accounts import ORDER_REMARK_PREFIX

		doc = _pe(remarks=f"{ORDER_REMARK_PREFIX}7788 — cheque 5")
		self._run(doc)
		self.assertEqual(doc.custom_remarks, 1)


class ARealSaveKeepsTheMarker(FrappeTestCase):
	"""The whole chain, against a real Payment Entry on a real site.

	Everything above stubs the document. That proves the handler decides
	correctly and proves nothing about the two claims the feature rests on:
	that Frappe reaches this handler before the controller rewrites the field,
	and that ERPNext really does rewrite it. Both are statements about somebody
	else's code, both are what an upgrade changes, and neither can be observed
	without letting a document through `insert()`.

	The negative control carries as much weight as the positive one: an entry
	that does NOT meet the conditions must come out with its remarks rewritten.
	The day that stops happening — ERPNext changes `set_remarks`, or the field
	stops being derived — this guard is dead weight, and this test says so
	rather than the whole file passing against a problem that no longer exists.
	"""

	MARKER = "GunBroker Order #99001 — refund cheque 40012"

	@classmethod
	def setUpClass(cls):
		# Needs a site. Skipped — never quietly passed — when there is no site
		# config at all, which is the offline stub harness the rest of this file
		# runs under. Anything else that goes wrong here is a real failure and
		# is allowed to be one: a bench site always has a Company.
		if not getattr(frappe, "conf", None):
			raise unittest.SkipTest(
				"needs a real site: bench --site <site> run-tests --app "
				"ffl_integrations --module "
				"ffl_integrations.tests.test_gunbroker_payment_remarks")
		super().setUpClass()
		cls.company = frappe.defaults.get_user_default("Company") or frappe.get_all(
			"Company", pluck="name", limit=1)[0]
		cls.customer = _ensure_party("Customer", "_Test GB Remarks Buyer")
		cls.supplier = _ensure_party("Supplier", "_Test GB Remarks Vendor")
		cls.bank = _a_cash_or_bank_account(cls.company)

	def _entry(self, party_type, party, payment_type="Pay"):
		from erpnext.accounts.party import get_party_account

		party_account = get_party_account(party_type, party, self.company)
		doc = frappe.get_doc({
			"doctype": "Payment Entry",
			"payment_type": payment_type,
			"company": self.company,
			"posting_date": frappe.utils.nowdate(),
			"party_type": party_type,
			"party": party,
			"paid_from": self.bank if payment_type == "Pay" else party_account,
			"paid_to": party_account if payment_type == "Pay" else self.bank,
			"paid_amount": 100.0,
			"received_amount": 100.0,
			"source_exchange_rate": 1,
			"target_exchange_rate": 1,
			"reference_no": "GBRM-1",
			"reference_date": frappe.utils.nowdate(),
			"custom_remarks": 0,
			"remarks": self.MARKER,
		})
		doc.flags.ignore_permissions = True
		doc.insert()
		self.addCleanup(frappe.delete_doc, "Payment Entry", doc.name,
			force=True, ignore_permissions=True)
		return doc

	def test_a_customer_refund_keeps_what_the_operator_typed(self):
		doc = self._entry("Customer", self.customer)
		self.assertEqual(doc.custom_remarks, 1)
		self.assertIn("GunBroker Order #99001", doc.remarks)

	def test_and_the_schedule_could_find_it_on_the_saved_row(self):
		# The end the marker exists for: read the stored value the way
		# _fetch_refund_entries does, match it the way _match_refunds does.
		from ffl_core.marketplace_accounts import ORDER_REMARK_PREFIX

		doc = self._entry("Customer", self.customer)
		saved = frappe.db.get_value("Payment Entry", doc.name, "remarks")
		self.assertIn(f"{ORDER_REMARK_PREFIX}99001", saved)

	def test_a_supplier_payment_has_its_remarks_rewritten(self):
		# The load-bearing negative: it pins the ERPNext behaviour the guard
		# exists because of. If this ever passes WITHOUT the rewrite, the guard
		# is obsolete rather than working, and the file should say so.
		doc = self._entry("Supplier", self.supplier)
		self.assertEqual(doc.custom_remarks, 0)
		self.assertNotIn("GunBroker Order #99001", doc.remarks or "")

	def test_a_receipt_from_a_customer_has_its_remarks_rewritten(self):
		doc = self._entry("Customer", self.customer, payment_type="Receive")
		self.assertEqual(doc.custom_remarks, 0)
		self.assertNotIn("GunBroker Order #99001", doc.remarks or "")


def _ensure_party(doctype, name):
	if frappe.db.exists(doctype, name):
		return name
	doc = frappe.get_doc({"doctype": doctype, f"{doctype.lower()}_name": name})
	if doctype == "Customer":
		doc.customer_group = frappe.get_all("Customer Group",
			filters={"is_group": 0}, pluck="name", limit=1)[0]
		doc.territory = frappe.get_all("Territory",
			filters={"is_group": 0}, pluck="name", limit=1)[0]
	else:
		doc.supplier_group = frappe.get_all("Supplier Group",
			filters={"is_group": 0}, pluck="name", limit=1)[0]
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _a_cash_or_bank_account(company):
	"""Any posting Cash or Bank account for the company.

	Cash first: production has no operating bank account at all yet (that is
	G5 #1), and the dev site is built from the same chart, so a Bank-type
	account is not something this test can assume."""
	for account_type in ("Cash", "Bank"):
		found = frappe.get_all("Account", pluck="name", limit=1, filters={
			"company": company, "account_type": account_type, "is_group": 0})
		if found:
			return found[0]
	raise AssertionError(f"no Cash or Bank account on {company}")


class ItRunsBeforeTheControllerNotAfter(FrappeTestCase):
	"""The registration, asserted on the event name.

	Registered on `validate` the guard still passes every test above — it sets
	the flag — while the text it exists to protect has already been rebuilt by
	`set_remarks()` earlier in the same save. Only the event name distinguishes
	a working guard from a decorative one.
	"""

	def test_it_is_registered_on_before_validate(self):
		self.assertIn("ffl_integrations.gunbroker.revenue.keep_gunbroker_remarks",
			hooks.doc_events["Payment Entry"]["before_validate"])

	def test_it_is_not_registered_on_validate(self):
		self.assertNotIn(
			"ffl_integrations.gunbroker.revenue.keep_gunbroker_remarks",
			hooks.doc_events["Payment Entry"].get("validate", []))

	def test_the_registration_is_a_list(self):
		# Same reason every other key in this file is: the next app to want a
		# Payment Entry handler must be able to append rather than replace.
		self.assertIsInstance(
			hooks.doc_events["Payment Entry"]["before_validate"], list)

	def test_the_bench_only_class_skips_on_a_precondition_not_always(self):
		# The real-save class cannot run here, so nothing offline can tell an
		# honest skip from `raise SkipTest` at the top of setUpClass — and an
		# unconditional skip is a test that has stopped existing without
		# anybody being told. Pinned at the source, which is the only level
		# this is visible at until the bench window.
		from pathlib import Path

		import ffl_integrations

		src = (Path(ffl_integrations.__file__).resolve().parent / "tests"
			/ "test_gunbroker_payment_remarks.py").read_text(encoding="utf-8")
		block = src[src.index("class ARealSaveKeepsTheMarker"):]
		block = block[:block.index("\n\ndef _ensure_party")]
		# The skip is conditional on there being no site config, and on nothing
		# else — an `if True:` or a bare raise would read the same in the runner
		# output ("skipped=1") as the honest version does.
		self.assertIn('if not getattr(frappe, "conf", None):', block)
		self.assertNotIn("if True:", block)
		self.assertEqual(block.count("SkipTest"), 1)

	def test_nothing_else_already_owned_this_event(self):
		# If a handler is ever added ahead of this one, it has to stay.
		self.assertEqual(
			hooks.doc_events["Payment Entry"]["before_validate"][-1],
			"ffl_integrations.gunbroker.revenue.keep_gunbroker_remarks")
