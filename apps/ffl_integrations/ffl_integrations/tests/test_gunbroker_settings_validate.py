"""GunBroker Settings validation — the two settings that fail silently.

Both of these were rules written in a go-live checklist, and both have the same
shape of failure: the form accepts the wrong value, nothing errors, and the
consequence surfaces weeks later somewhere that does not point back here.

* **Check Deposit Account.** The picker offers the clearing accounts right
  alongside the real bank account — they are Bank-type too, and they are the
  nearest-looking wrong answers on the list. Choose one and cheque deposits and
  card settlements pile into a single account; the month-end bank reconciliation
  simply stops matching, with nothing to say why.
* **Auto-Relist.** `2` is the auction value. On a fixed-price listing it does
  nothing at all, so every unsold gun quietly falls off GunBroker after thirty
  days — a silence nobody reports as a bug.

The Select no longer offers `2`, so the only ways to reach that check are a
value written through the API and a row that predates the change. Both are
worth catching, and neither would be noticed without this.
"""
from __future__ import annotations

import types
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.ffl_integrations.doctype.gunbroker_settings import (
	gunbroker_settings as module,
)

COMPANY = "_Test Store A"
ABBR = "TSA"
REAL_BANK = f"Operating Bank - {ABBR}"

ACCOUNTS = {
	REAL_BANK: {"account_type": "Bank", "is_group": 0, "company": COMPANY},
	f"Woo Clearing - {ABBR}": {"account_type": "Bank", "is_group": 0, "company": COMPANY},
	f"Payroc Card Clearing - {ABBR}": {
		"account_type": "Bank", "is_group": 0, "company": COMPANY},
	f"Bank Accounts - {ABBR}": {"account_type": "Bank", "is_group": 1, "company": COMPANY},
	f"Accounts Receivable - {ABBR}": {
		"account_type": "Receivable", "is_group": 0, "company": COMPANY},
}


class _SettingsDouble(types.SimpleNamespace):
	"""Fields plus the controller's real validation methods."""

	_require_deposit_account = module.GunBrokerSettings._require_deposit_account
	_require_real_bank_account = module.GunBrokerSettings._require_real_bank_account
	_warn_about_auto_relist = module.GunBrokerSettings._warn_about_auto_relist
	on_update = module.GunBrokerSettings.on_update
	_sync_modes_of_payment = module.GunBrokerSettings._sync_modes_of_payment


class _ValidateCase(FrappeTestCase):
	"""Drives the controller's own methods against a settings double.

	Calling the bound methods directly rather than saving a document: this is
	about the rules, and a real save would drag in the whole Single write path
	plus a chart of accounts that only exists on a configured site.
	"""

	def setUp(self):
		self.thrown: list[str] = []

		def _throw(msg="", **_kw):
			self.thrown.append(str(msg))
			raise frappe.ValidationError(str(msg))

		self.messages: list[str] = []

		def _msgprint(msg="", **_kw):
			self.messages.append(str(msg))

		for target, kw in (("frappe.throw", {"side_effect": _throw}),
				("frappe.msgprint", {"side_effect": _msgprint})):
			patcher = patch(target, **kw)
			patcher.start()
			self.addCleanup(patcher.stop)

		def _get_value(doctype, name, fields=None, **_kw):
			if doctype == "Company":
				return ABBR
			if doctype == "Account":
				return frappe._dict(ACCOUNTS.get(name) or {})
			return None

		patcher = patch("frappe.db.get_value", side_effect=_get_value)
		patcher.start()
		self.addCleanup(patcher.stop)

	def _settings(self, **over):
		"""A stand-in carrying the REAL methods, not mocked ones.

		A MagicMock would answer `self._require_real_bank_account()` with another
		mock, so the delegated half of the check would never run and every one of
		these tests would pass while asserting nothing.
		"""
		doc = _SettingsDouble()
		doc.enabled = 1
		doc.auto_relist = "4 Relist Fixed Price"
		doc.check_deposit_account = REAL_BANK
		for key, value in over.items():
			setattr(doc, key, value)
		return doc

	def _deposit(self, account):
		self._settings(check_deposit_account=account)._require_deposit_account()

	def _auto_relist(self, value):
		self._settings(auto_relist=value)._warn_about_auto_relist()


class CheckDepositAccount(_ValidateCase):
	def test_a_real_operating_bank_account_is_accepted(self):
		self._deposit(REAL_BANK)
		self.assertEqual(self.thrown, [])

	def test_empty_is_refused_while_enabled(self):
		"""This company has no default bank account and no operating bank account
		in its chart, so there is nothing to fall back to: every payment would
		fail at the moment somebody records money already in hand."""
		with self.assertRaises(frappe.ValidationError):
			self._deposit("")
		self.assertIn("Check Deposit Account", self.thrown[0])

	def test_a_clearing_account_is_refused(self):
		"""Bank-type, sitting right next to the right answer in the picker,
		and catastrophic to pick — the failure only shows up at month-end."""
		with self.assertRaises(frappe.ValidationError):
			self._deposit(f"Woo Clearing - {ABBR}")
		self.assertIn("clearing account", self.thrown[0])
		self.assertIn("reconciliation", self.thrown[0])

	def test_the_card_clearing_account_is_refused_too(self):
		with self.assertRaises(frappe.ValidationError):
			self._deposit(f"Payroc Card Clearing - {ABBR}")
		self.assertIn("clearing account", self.thrown[0])

	def test_a_group_account_is_refused(self):
		"""Nothing can be posted to a group account, so this one fails on
		the first payment rather than at month-end — but it fails at the till."""
		with self.assertRaises(frappe.ValidationError):
			self._deposit(f"Bank Accounts - {ABBR}")
		self.assertIn("group account", self.thrown[0])

	def test_a_non_bank_account_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._deposit(f"Accounts Receivable - {ABBR}")
		self.assertIn("not a Bank account", self.thrown[0])

	def test_each_refusal_says_which_rule_was_broken(self):
		"""Three different mistakes, three different messages. "Invalid account"
		would leave somebody guessing which of them they hit."""
		seen = set()
		for account in (f"Woo Clearing - {ABBR}", f"Bank Accounts - {ABBR}",
				f"Accounts Receivable - {ABBR}"):
			self.thrown.clear()
			with self.assertRaises(frappe.ValidationError):
				self._deposit(account)
			seen.add(self.thrown[0])
		self.assertEqual(len(seen), 3)

	def test_nothing_is_enforced_while_the_integration_is_off(self):
		"""A store that has not switched GunBroker on has no reason to have
		configured its settlement account yet."""
		module.GunBrokerSettings._require_deposit_account(
			self._settings(enabled=0, check_deposit_account=""))
		self.assertEqual(self.thrown, [])


class AutoRelist(_ValidateCase):
	def test_the_two_working_values_are_silent(self):
		for value in module.VALID_AUTO_RELIST:
			with self.subTest(value):
				self.messages.clear()
				self._auto_relist(value)
				self.assertEqual(self.messages, [])

	def test_the_auction_value_is_called_out(self):
		"""Not offered by the Select any more, so this catches an API write
		or a row from before that change. On a fixed-price listing 2 does
		nothing, and "does nothing" here means every unsold gun disappears from
		GunBroker after 30 days with no error anywhere."""
		self._auto_relist("2 Relist Until Sold")
		self.assertEqual(len(self.messages), 1)
		self.assertIn("auction", self.messages[0])
		self.assertIn("4 Relist Fixed Price", self.messages[0])

	def test_a_blank_or_unknown_value_is_called_out(self):
		for value in ("", "3 Something", None):
			with self.subTest(value):
				self.messages.clear()
				self._auto_relist(value)
				self.assertEqual(len(self.messages), 1)

	def test_the_select_no_longer_offers_the_auction_value(self):
		"""The validate check is the backstop; the real fix is that nobody can
		pick it. Read off the doctype JSON so a re-added option fails here."""
		import json
		from pathlib import Path

		schema = json.loads(
			(Path(module.__file__).parent / "gunbroker_settings.json")
			.read_text(encoding="utf-8"))
		field = next(f for f in schema["fields"] if f["fieldname"] == "auto_relist")
		options = [o for o in field["options"].split("\n") if o.strip()]
		self.assertEqual(options, list(module.VALID_AUTO_RELIST))
		self.assertEqual(field["default"], "4 Relist Fixed Price")


class SavingTheFormPointsTheModesOfPayment(FrappeTestCase):
	"""The accounts on this form were decorative without this.

	The seed that creates the two GunBroker Modes of Payment and points them at
	the accounts named here runs on install and on migrate, and nothing else
	called it. On the ordinary go-live order — migrate an empty site, then fill
	the deposit account in — `GunBroker Check` was therefore never created at
	all: the first Payment Entry throws for want of an account, with the gun
	already shipped and the cheque already banked. The same gap made a later
	change of deposit account silent, every receipt still landing in the old one.
	"""

	def test_saving_re_points_the_modes_for_every_company(self):
		settings = _SettingsDouble()
		with patch("frappe.get_all", return_value=["_Test Store A", "TSB"]), \
				patch("ffl_integrations.gunbroker.setup.ensure_modes_of_payment"
					) as ensure:
			settings.on_update()
		self.assertEqual([c.args[0] for c in ensure.call_args_list],
			["_Test Store A", "TSB"])

	def test_a_chart_of_accounts_that_is_half_built_does_not_block_the_save(self):
		# This is a settings form. Refusing to save it because one company's
		# accounts are not ready yet would leave the operator unable to fill in
		# the very field that fixes it.
		settings = _SettingsDouble()
		with patch("frappe.get_all", return_value=["_Test Store A"]), \
				patch("ffl_integrations.gunbroker.setup.ensure_modes_of_payment",
					side_effect=RuntimeError("no such parent account")), \
				patch("frappe.log_error") as log:
			settings.on_update()  # must not raise
		log.assert_called_once()
