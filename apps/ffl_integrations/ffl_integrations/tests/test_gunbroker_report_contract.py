"""The Sales Report's two GunBroker couplings, pinned from this side.

ffl_core has to run on a site where ffl_integrations is not installed, so the
report cannot import the integration to find out what a GunBroker account is
called or what channels exist. It duplicates both facts instead, with a comment
saying why. Duplication with a reason is fine; duplication with nothing
checking it is how the two copies drift apart, and both drifts are silent:

* rename an account on one side and every GunBroker tax row falls into
  ``unclassified``. The report still renders, the Data Flags panel goes red,
  and the number that is wrong is the one the Texas return is reconciled
  against.
* add a channel on one side and the filter that would select it never receives
  the value, because the only thing that sends it is a Desk Select whose
  options are written out by hand.

The tests live here rather than in ffl_core because that is the app that may be
absent; from this side both ends are guaranteed to exist.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import ffl_core
from ffl_core import marketplace_accounts as ma
from ffl_core.ffl_core.report.sales_report import sales_report

from ffl_integrations.gunbroker import setup as gb_setup

_REPORT_JS = (Path(ffl_core.__file__).resolve().parent / "ffl_core" / "report"
	/ "sales_report" / "sales_report.js")


class MarketplaceAccountNamesAgree(unittest.TestCase):
	def test_the_tax_liability_is_the_account_the_integration_creates(self):
		self.assertIn(gb_setup.MPF_TAX_ACCOUNT,
			sales_report._MARKETPLACE_TAX_ACCOUNTS)

	def test_the_fee_liability_is_the_account_the_integration_creates(self):
		self.assertIn(gb_setup.MSF_ACCOUNT,
			sales_report._MARKETPLACE_FEE_ACCOUNTS)

	def test_neither_set_names_an_account_nothing_creates(self):
		# The other direction: a leftover name in the report is a row that will
		# be classified as a marketplace liability and never posted to.
		known = {gb_setup.MPF_TAX_ACCOUNT, gb_setup.MSF_ACCOUNT}
		self.assertEqual(
			(sales_report._MARKETPLACE_TAX_ACCOUNTS
				| sales_report._MARKETPLACE_FEE_ACCOUNTS) - known,
			set())


class TheChannelFilterCanSelectEveryChannel(unittest.TestCase):
	"""Every channel _channel() returns has to be offerable in the Desk filter.

	The server side of the GunBroker filter was written and worked; the Select
	that is the only thing able to send it still listed three values, so the
	branch was unreachable and the store could not look at GunBroker sales on
	their own.
	"""

	def _options(self) -> list[str]:
		source = _REPORT_JS.read_text(encoding="utf-8")
		match = re.search(r'fieldname:\s*"channel".*?options:\s*\[(.*?)\]',
			source, re.S)
		self.assertIsNotNone(match, "the channel filter's options are not a literal")
		return re.findall(r'"([^"]*)"', match.group(1))

	def test_the_report_js_was_found(self):
		self.assertTrue(_REPORT_JS.is_file(), _REPORT_JS)

	def test_every_channel_the_server_derives_is_on_the_dropdown(self):
		derived = {
			sales_report._channel("POS Invoice", None, None),
			sales_report._channel("Sales Invoice", "WOO-ORD-1", None),
			sales_report._channel("Sales Invoice", None, "GB-ORD-1"),
			sales_report._channel("Sales Invoice", None, None),
		}
		self.assertEqual(derived,
			{"POS", "Web", "GunBroker", "Manual"})  # the four it can return
		missing = derived - set(self._options())
		self.assertEqual(missing, set(),
			"channels the server filters on that the Desk cannot send")

	def test_the_dropdown_offers_nothing_the_server_cannot_filter_on(self):
		# A value the filter does not recognise returns an empty report with no
		# explanation. "" is "all" and is not a channel.
		offered = {o for o in self._options() if o}
		self.assertEqual(offered - {"POS", "Web", "GunBroker", "Manual"}, set())


class EveryDuplicatedNameAgrees(unittest.TestCase):
	"""ffl_core.marketplace_accounts is the whole of what ffl_core is allowed to
	know about this integration. Every entry in it is a copy, and every copy
	fails silently when it drifts:

	* an account name → the month-end schedule reports a zero balance on an
	  account that has money in it, or the Sales Report drops the tax row into
	  ``unclassified``;
	* the order doctype → the marketplace-exemption read-back finds nothing and
	  every $0-tax row looks like a no-tax state;
	* the remark marker → the cash-basis schedule never sees a refund, so a
	  refunded order stays in gross receipts for good.

	None of these raise. So they are pinned here, from the app that may be
	absent, in both directions.
	"""

	def test_the_accrued_fee_account_is_the_one_setup_creates(self):
		self.assertEqual(ma.ACCRUED_FEES_ACCOUNT, gb_setup.ACCRUED_FEES_ACCOUNT)

	def test_the_fee_expense_account_is_the_one_setup_creates(self):
		self.assertEqual(ma.FEES_EXPENSE_ACCOUNT, gb_setup.FEES_EXPENSE_ACCOUNT)

	def test_the_two_liability_names_are_the_ones_setup_creates(self):
		self.assertEqual(ma.MPF_TAX_ACCOUNT, gb_setup.MPF_TAX_ACCOUNT)
		self.assertEqual(ma.MSF_ACCOUNT, gb_setup.MSF_ACCOUNT)

	def test_the_settings_singleton_is_named_the_same_on_both_sides(self):
		self.assertEqual(ma.SETTINGS_DOCTYPE, gb_setup.SETTINGS_DOCTYPE)

	def test_the_order_doctype_is_named_the_same_on_both_sides(self):
		from ffl_integrations.gunbroker import revenue as gb_revenue
		self.assertEqual(ma.ORDER_DOCTYPE, gb_revenue.ORDER_DOCTYPE)

	def test_the_deposit_account_field_the_schedule_reads_exists_on_settings(self):
		# item 7 names the debit side by reading this field off the Settings
		# single. A rename would leave the year-end entry addressed to a
		# placeholder, which reads as "not configured yet".
		path = (Path(gb_setup.__file__).resolve().parent.parent / "ffl_integrations"
			/ "doctype" / "gunbroker_settings" / "gunbroker_settings.json")
		self.assertIn('"fieldname": "check_deposit_account"',
			path.read_text(encoding="utf-8"))

	def test_the_remark_marker_is_what_the_receipt_actually_writes(self):
		# The refund lookup keys off this prefix. Pinned against the literal in
		# revenue.py rather than a second copy of the sentence.
		source = (Path(gb_setup.__file__).resolve().parent
			/ "revenue.py").read_text(encoding="utf-8")
		self.assertIn(ma.ORDER_REMARK_PREFIX + "{0}", source)

	def test_the_marker_is_specific_enough_to_key_on(self):
		# "#" is doing real work: without the terminator the digit guard in
		# _match_refunds has nothing to guard.
		self.assertTrue(ma.ORDER_REMARK_PREFIX.endswith("#"))
		self.assertIn("GunBroker", ma.ORDER_REMARK_PREFIX)
