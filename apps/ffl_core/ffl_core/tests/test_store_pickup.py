"""In-store pickup: the buyer collects at the counter, so there is no far-end FFL.

A web order can name the store's OWN licence as the receiving FFL — WooCommerce's
FFL picker lists us like any other dealer, and a local buyer picks us.
Nothing in POS recognised that shape, and both halves of the flow got it wrong:

* `dispose_web_order` would have booked `FFL Transfer -> TEST STORE A INC`:
  a transfer to ourselves, which never happened, while the REAL disposition — a
  retail sale to a non-licensee, with a 4473 — went unrecorded. Covered in
  ffl_woo_sync's own suite; `is_own_license` here is the predicate it uses.

* the revenue Sales Invoice could never submit. `require_4473_and_nics_for_firearm_sale`
  reads `form_4473_status` / `nics_status` off the document — fields that exist on
  POS Invoice and NOT on Sales Invoice — so for an SI the gate can only ever be
  satisfied by the `ffl_destination_ffl` escape. A pickup has no destination FFL,
  so the gate was unpassable and the sale's revenue and sales-tax liability simply
  could not be booked. That was not specific to pickups: ANY web firearm order
  without a destination FFL hit it.

The fix is not to weaken the gate but to let it read the proof where a web retail
4473 actually records it — a submitted Retail Sale FFL Disposition against the
same Woo order. That proof is strong: `FFL Disposition._reject_manual_retail_sale`
refuses a hand-made Retail Sale unless it carries a FastBound id (i.e. came from
the 4473 webhook) or an audited manager override.

Coverage for both invoice kinds is answered by ONE filter set, because the
existing `covered_serials_for_invoice` docstring warns in as many words that a
second copy of these criteria must never be allowed to drift from the first.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

MOD = "ffl_core.compliance.firearm"
OWN = "5-99-888-01-9B-42763"


def _row(item_code="PISTOL-1", serial_no="X100001", qty=1):
	return frappe._dict({"item_code": item_code, "serial_no": serial_no,
		"qty": qty, "idx": 1})


def _si(*, serials="X100001", woo_online_order="WOO-ORD-00008",
		destination_ffl=None, docstatus=1):
	doc = MagicMock()
	doc.doctype = "Sales Invoice"
	doc.name = "ACC-SINV-2026-00001"
	doc.docstatus = docstatus
	doc.items = [_row(serial_no=serials)]
	doc.woo_online_order = woo_online_order
	doc.ffl_destination_ffl = destination_ffl
	doc.customer = "Sam Sample"
	# A MagicMock invents a truthy value for anything unset, and the gate's first
	# escape is `if doc.is_consolidated: return` — leaving it unset would make
	# every one of these tests pass by never reaching the code under test.
	doc.is_consolidated = 0
	# Sales Invoice genuinely has neither field — the whole reason the gate was
	# unpassable. MagicMock would invent them, so pin them absent.
	del doc.form_4473_status
	del doc.nics_status
	return doc


# ---------------------------------------------------------------------------
# is_own_license
# ---------------------------------------------------------------------------

class TestIsOwnLicense(FrappeTestCase):
	def _is_own(self, value, configured=OWN):
		from ffl_core.firearm import is_own_license
		rows = [(configured,)] if configured else []
		with patch("frappe.db.sql", return_value=rows):
			return is_own_license(value)

	def test_exact_match(self):
		self.assertTrue(self._is_own(OWN))

	def test_punctuation_and_case_do_not_defeat_it(self):
		"""WooCommerce hands the licence back in whatever shape the picker had."""
		self.assertTrue(self._is_own("599888019b42763"))
		self.assertTrue(self._is_own(" 5-99-888-01-9B-42763 "))
		self.assertTrue(self._is_own("5 99 888 01 9b 42763"))

	def test_another_dealer_is_not_us(self):
		self.assertFalse(self._is_own("3-99-888-01-9G-09099"))

	def test_blank_input_is_not_us(self):
		self.assertFalse(self._is_own(""))
		self.assertFalse(self._is_own(None))

	def test_it_touches_neither_the_orm_nor_the_meta_cache(self):
		"""The regression guard. `frappe.db.get_single_value` resolves the field
		through `frappe.get_meta`, which on a COLD cache loads the DocType via
		the ORM. This runs inside `list_pending_web_orders`, which a dozen tests
		exercise with `frappe.get_all` / `frappe.get_doc` patched — so it fed the
		mock's rows to Meta and died. A warm meta cache hid it on the dev site
		and CI's fresh site found it. Reading tabSingles directly is what keeps
		this off the ORM, so assert it stays that way."""
		from ffl_core.firearm import is_own_license
		with patch("frappe.db.sql", return_value=[(OWN,)]), \
			 patch("frappe.get_all", side_effect=AssertionError("used the ORM")), \
			 patch("frappe.get_doc", side_effect=AssertionError("used the ORM")), \
			 patch("frappe.get_meta", side_effect=AssertionError("used the meta cache")), \
			 patch("frappe.db.get_single_value",
				 side_effect=AssertionError("goes through get_meta")):
			self.assertTrue(is_own_license(OWN))

	def test_unconfigured_setting_never_matches(self):
		"""Until someone fills the setting in, EVERY order must keep its existing
		behaviour — an empty setting matching an empty licence would silently
		reroute every order with no FFL into the pickup lane."""
		self.assertFalse(self._is_own(OWN, configured=""))
		self.assertFalse(self._is_own("", configured=""))
		self.assertFalse(self._is_own(OWN, configured=None))


# ---------------------------------------------------------------------------
# covered_serials_* — one filter set, two link keys
# ---------------------------------------------------------------------------

class TestCoverageFilters(FrappeTestCase):
	def _filters_used(self, fn, link_value):
		from ffl_core.compliance import firearm
		with patch("frappe.get_all", return_value=[]) as get_all:
			getattr(firearm, fn)(link_value, ["X100001"])
		return get_all.call_args[1]["filters"]

	def test_both_kinds_demand_identical_proof(self):
		"""The only difference between them may be WHICH document the disposition
		hangs off. Any other divergence is a hole in one of the two gates."""
		pos = self._filters_used("covered_serials_for_invoice", "ACC-PSINV-1")
		web = self._filters_used("covered_serials_for_web_order", "WOO-ORD-00008")
		self.assertEqual(pos.pop("linked_pos_invoice"), "ACC-PSINV-1")
		self.assertEqual(web.pop("woo_online_order"), "WOO-ORD-00008")
		self.assertEqual(pos, web)

	def test_proof_is_a_submitted_retail_sale_with_4473_and_nics(self):
		web = self._filters_used("covered_serials_for_web_order", "WOO-ORD-00008")
		self.assertEqual(web["disposition_type"], "Retail Sale")
		self.assertEqual(web["form_4473_status"], "Completed")
		self.assertEqual(web["nics_status"], ("in", ["Proceed"]))
		# Draft = not in the bound book yet; cancelled = voided. Neither is proof.
		self.assertEqual(web["docstatus"], 1)

	def test_no_serials_asks_the_database_nothing(self):
		from ffl_core.compliance.firearm import covered_serials_for_web_order
		with patch("frappe.get_all") as get_all:
			self.assertEqual(covered_serials_for_web_order("WOO-ORD-1", []), set())
		get_all.assert_not_called()


# ---------------------------------------------------------------------------
# the gate itself
# ---------------------------------------------------------------------------

class TestWebRetail4473Gate(FrappeTestCase):
	def _run(self, doc, *, covered=("X100001",)):
		from ffl_core.compliance.firearm import require_4473_and_nics_for_firearm_sale
		with patch("frappe.db.get_value", return_value=None), \
			 patch(f"{MOD}.is_regulated_firearm_item", return_value=True), \
			 patch(f"{MOD}.covered_serials_for_web_order",
				 return_value=set(covered)) as cov:
			require_4473_and_nics_for_firearm_sale(doc)
		return cov

	def test_a_covered_pickup_invoice_submits(self):
		"""The 4473 happened; its proof is the submitted Retail Sale disposition
		on this web order. Nothing about that is weaker than the POS path."""
		cov = self._run(_si())
		cov.assert_called_once()
		self.assertEqual(cov.call_args[0][0], "WOO-ORD-00008")

	def test_an_uncovered_invoice_is_still_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._run(_si(), covered=())

	def test_a_serial_added_after_the_4473_is_refused(self):
		"""Coverage is per SERIAL, so swapping or adding a gun after the fact
		cannot ride on the first gun's 4473."""
		doc = _si()
		doc.items = [_row(serial_no="X100001"), _row(serial_no="B999999")]
		with self.assertRaises(frappe.ValidationError):
			self._run(doc, covered=("X100001",))

	def test_an_invoice_with_no_web_order_gets_no_free_pass(self):
		"""A hand-made Sales Invoice for a firearm has no web 4473 behind it."""
		with self.assertRaises(frappe.ValidationError):
			self._run(_si(woo_online_order=None), covered=("X100001",))

	def test_a_shipped_order_still_skips_on_destination_ffl(self):
		"""The ordinary web sale: the gun is mailed to a dealer who runs the
		4473. Unchanged — and it must not start demanding coverage."""
		from ffl_core.compliance.firearm import require_4473_and_nics_for_firearm_sale
		with patch("frappe.db.get_value", return_value=None), \
			 patch(f"{MOD}.is_regulated_firearm_item", return_value=True), \
			 patch(f"{MOD}.covered_serials_for_web_order") as cov:
			require_4473_and_nics_for_firearm_sale(_si(destination_ffl="FFL-0001"))
		cov.assert_not_called()

	def test_a_draft_is_never_gated(self):
		from ffl_core.compliance.firearm import require_4473_and_nics_for_firearm_sale
		with patch("frappe.db.get_value", return_value=None), \
			 patch(f"{MOD}.is_regulated_firearm_item", return_value=True), \
			 patch(f"{MOD}.covered_serials_for_web_order") as cov:
			require_4473_and_nics_for_firearm_sale(_si(docstatus=0))
		cov.assert_not_called()

	def test_pos_invoice_keeps_reading_its_own_fields(self):
		"""The counter path carries real 4473 fields and its own per-serial
		coverage check. The web escape must not apply to it — a POS Invoice has
		no woo_online_order, and reaching for one would be a new hole."""
		from ffl_core.compliance.firearm import require_4473_and_nics_for_firearm_sale
		doc = MagicMock()
		doc.doctype = "POS Invoice"
		doc.docstatus = 1
		doc.items = [_row()]
		doc.ffl_destination_ffl = None
		doc.customer = "Walk In"
		doc.is_consolidated = 0
		doc.form_4473_status = ""
		doc.nics_status = ""
		with patch("frappe.db.get_value", return_value=None), \
			 patch(f"{MOD}.is_regulated_firearm_item", return_value=True), \
			 patch(f"{MOD}.covered_serials_for_web_order") as cov, \
			 self.assertRaises(frappe.ValidationError):
			require_4473_and_nics_for_firearm_sale(doc)
		cov.assert_not_called()
