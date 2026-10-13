"""``ffl_core.setup.store_setup`` — one site, one store, idempotently.

Runs against the dev site's own Company with made-up store facts (nothing
here is a real store); every write rolls back at class end. The contract under
test: everything the receipts / letter head / POS / mail read is converged from
``config`` alone, a second run is silent, and a missing required fact refuses
before writing.
"""
from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.setup import store_setup
from ffl_core.store_identity import company_brand


def _company() -> tuple[str, str]:
	name = (frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		or frappe.db.get_single_value("Global Defaults", "default_company"))
	return name, frappe.db.get_value("Company", name, "abbr")


def _config(**over) -> dict:
	name, abbr = _company()
	c = {
		"COMPANY_NAME": name, "COMPANY_ABBR": abbr, "OWNER_EMAIL": "owner@example.test",
		"STORE_PHONE": "(555) 010-0000", "STORE_WEBSITE": "https://example.test/",
		"ADDRESS_LINE1": "1 Test St", "ADDRESS_LINE2": "", "CITY": "Testville", "STATE": "TX",
		"PINCODE": "75001", "FFL_NUMBER": "1-11-111-11-1A-11111", "CFD_NUMBER": "",
		"LOGO_URL": "/files/_test-store-logo.png", "RECEIPT_FOOTER_NOTE": "ALL SALES ARE FINAL.",
		"SALES_TAX_RATE": "8.25", "SALES_TAX_TITLE": "_Test Sales Tax 8.25%",
		"ITT_TITLE": "_Test Taxable Goods", "CFET_RATE": "0",
		"SMTP_SERVER": "smtp.example.test", "SMTP_LOGIN": "pos@example.test",
		"SMTP_PASSWORD": "not-a-real-secret", "SMTP_ACCOUNT_NAME": "_Test Store Mail",
	}
	c.update(over)
	return c


def _run(config: dict):
	out = io.StringIO()
	with contextlib.redirect_stdout(out):
		result = store_setup.run(config)
	return result, out.getvalue()


class StoreSetupConverges(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		from frappe.email.doctype.email_account.email_account import EmailAccount
		# No network in tests; the account's own SMTP probe is not what is under test.
		p = patch.object(EmailAccount, "validate_smtp_conn", lambda self, *a, **k: None, create=True)
		p.start()
		cls.addClassCleanup(p.stop)
		cls.name, cls.abbr = _company()
		cls.cfg = _config()
		cls.result, cls.first_log = _run(cls.cfg)

	def test_letter_head_is_default_with_logo_website_licence_and_default_key(self):
		lh = frappe.get_doc("Letter Head", self.abbr)
		self.assertEqual(lh.is_default, 1)
		self.assertIn('<img src="/files/_test-store-logo.png"', lh.content)
		self.assertIn("example.test", lh.content)
		self.assertIn("FFL 1-11-111-11-1A-11111", lh.content)
		self.assertNotIn("CFD#", lh.content, "blank CFD number must omit the line")
		self.assertEqual(frappe.defaults.get_global_default("letter_head"), self.abbr,
			"new documents read the DefaultValue key, not the flag")

	def test_pos_profile_gets_receipt_format_letter_head_and_company_address(self):
		p = frappe.db.get_value("POS Profile", self.abbr,
			["letter_head", "print_format", "company_address", "taxes_and_charges"], as_dict=True)
		self.assertEqual(p.letter_head, self.abbr)
		self.assertEqual(p.print_format, "POS Receipt")
		self.assertTrue(p.company_address)
		self.assertTrue(frappe.db.get_value("Address", p.company_address, "is_your_company_address"))
		self.assertEqual(p.taxes_and_charges, self.result["state"]["default_template"])

	def test_branding_singles_company_fields_and_receipt_footer(self):
		self.assertEqual(frappe.db.get_single_value("System Settings", "disable_standard_email_footer"), 1)
		self.assertIn("1 Test St", frappe.db.get_single_value("System Settings", "email_footer_address"))
		self.assertEqual(frappe.db.get_single_value("Website Settings", "app_name"), self.name)
		self.assertEqual(frappe.db.get_value("Company", self.name, "phone_no"), "(555) 010-0000")
		self.assertEqual(company_brand(self.name)["phone"], "(555) 010-0000")
		self.assertEqual(frappe.db.get_single_value("FFL Settings", "receipt_footer_note"),
			"ALL SALES ARE FINAL.")

	def test_email_account_sender_flags(self):
		row = frappe.db.get_value("Email Account", "_Test Store Mail",
			["always_use_account_name_as_sender_name", "always_use_account_email_id_as_sender",
				"default_outgoing"], as_dict=True)
		self.assertEqual((row.always_use_account_name_as_sender_name,
			row.always_use_account_email_id_as_sender, row.default_outgoing), (1, 1, 1))

	def test_a_default_bank_account_that_is_already_set_is_left_alone(self):
		other = f"Operating Bank - {self.abbr}"
		if not frappe.db.exists("Account", other):
			frappe.get_doc({"doctype": "Account", "account_name": "Operating Bank", "company": self.name, "is_group": 0,
				"parent_account": f"Bank Accounts - {self.abbr}", "account_type": "Bank"}).insert(ignore_permissions=True)
		frappe.db.set_value("Company", self.name, "default_bank_account", other)
		_run(self.cfg)
		self.assertEqual(frappe.db.get_value("Company", self.name, "default_bank_account"), other)

	def test_no_excise_account_when_rate_is_zero(self):
		self.assertFalse(frappe.db.exists("Account", f"CA Firearm Excise Tax Payable - {self.abbr}"))

	def test_second_run_is_silent_and_committed_marker_printed(self):
		_, log = _run(self.cfg)
		noise = [l for l in log.splitlines() if l.startswith("STORE_SETUP")
			and not any(k in l for k in ("left alone", "tax.ensure", "SMTP not configured", "COMMITTED"))]
		self.assertEqual(noise, [], f"a second run must not rewrite anything: {noise}")
		self.assertIn(store_setup.MARKER, log)

	def test_disabled_company_address_is_replaced_not_reused(self):
		old = frappe.db.get_value("POS Profile", self.abbr, "company_address")
		frappe.db.set_value("Address", old, "disabled", 1)
		new = None
		try:
			_run(self.cfg)
			new = frappe.db.get_value("POS Profile", self.abbr, "company_address")
			self.assertNotEqual(new, old)
			self.assertFalse(frappe.db.get_value("Address", new, "disabled"))
		finally:
			# hand the class its original state back (tests in here share one run)
			frappe.db.set_value("Address", old, "disabled", 0)
			frappe.db.set_value("POS Profile", self.abbr, "company_address", old)
			if new and new != old:
				frappe.delete_doc("Address", new, force=True, ignore_permissions=True)


def _config_no_mail(**over) -> dict:
	return _config(SMTP_SERVER="", SMTP_LOGIN="", SMTP_PASSWORD="", **over)


class StoreSetupNewSite(FrappeTestCase):
	"""What a newcomer's fresh site lacks after the ERPNext wizard: serial tracking, the cash drawer's
	accounts, a 4473 choice. Each is written once and a re-run on a configured site writes nothing."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.name, cls.abbr = _company()

	def test_serial_numbers_for_items_are_switched_on_once(self):
		field = "enable_serial_and_batch_no_for_item"
		frappe.db.set_single_value("Stock Settings", field, 0)
		frappe.db.set_default(field, 0)
		_, log = _run(_config_no_mail())
		self.assertEqual(frappe.db.get_single_value("Stock Settings", field), 1)
		self.assertEqual(frappe.db.get_default(field), "1", "the Desk reads the default, not the single")
		self.assertIn(f"stock settings {field} -> 1", log)
		_, log = _run(_config_no_mail())
		self.assertNotIn("stock settings", log, "a site that has it on must not be written again")

	def test_a_site_with_only_one_of_the_two_serial_settings_is_repaired(self):
		field = "enable_serial_and_batch_no_for_item"
		for single, default in ((1, 0), (0, 1)):
			frappe.db.set_single_value("Stock Settings", field, single)
			frappe.db.set_default(field, default)
			_, log = _run(_config_no_mail())
			self.assertEqual((frappe.db.get_single_value("Stock Settings", field), frappe.db.get_default(field)),
				(1, "1"), f"single={single} default={default}")
			self.assertIn("stock settings", log)

	def test_dev_site_uses_the_same_helper(self):
		from ffl_core.setup import dev_site, serial_tracking
		frappe.db.set_single_value("Stock Settings", serial_tracking.FIELD, 0)
		frappe.db.set_default(serial_tracking.FIELD, 0)
		dev_site._stock_settings()
		self.assertTrue(serial_tracking.is_on())

	def test_the_cash_tender_is_the_account_the_cash_drawer_uses(self):
		from ffl_core.cash_drawer import cash_account
		alt = f"Till Cash - {self.abbr}"
		if not frappe.db.exists("Account", alt):
			frappe.get_doc({"doctype": "Account", "account_name": "Till Cash", "company": self.name, "is_group": 0,
				"parent_account": f"Cash In Hand - {self.abbr}", "account_type": "Cash"}).insert(ignore_permissions=True)
		before = frappe.db.get_value("Company", self.name, "default_cash_account")
		frappe.db.set_value("Company", self.name, "default_cash_account", alt)
		try:
			_run(_config_no_mail())
			tender = frappe.db.get_value("Mode of Payment Account", {"parent": "Cash", "company": self.name}, "default_account")
			self.assertEqual((tender, cash_account(self.name)), (alt, alt))
		finally:
			frappe.db.set_value("Company", self.name, "default_cash_account", before)

	def test_a_missing_account_is_named_not_returned(self):
		c = store_setup._normalize(_config_no_mail())
		before = frappe.db.get_value("Company", self.name, "write_off_account")
		frappe.db.set_value("Company", self.name, "write_off_account", None)
		try:
			with patch("ffl_core.setup.store_setup.frappe.db.exists", return_value=False):
				with self.assertRaises(frappe.ValidationError) as ctx:
					store_setup._account_or_default(c, "Write Off", "write_off_account")
		finally:
			frappe.db.set_value("Company", self.name, "write_off_account", before)
		self.assertIn(f"Write Off - {self.abbr}", str(ctx.exception))
		self.assertIn("write_off_account", str(ctx.exception))

	def test_a_missing_account_refuses_before_anything_is_written(self):
		abbr = "TNW"
		company = frappe.get_doc({
			"doctype": "Company", "company_name": "_Test No Write Off Co", "abbr": abbr, "default_currency": "USD",
			"country": "United States", "create_chart_of_accounts_based_on": "Standard Template",
			"chart_of_accounts": "Standard with Numbers"}).insert(ignore_permissions=True)
		frappe.db.set_value("Company", company.name, "write_off_account", None)  # no "Write Off - TNW" either: numbered chart
		cfg = _config_no_mail(COMPANY_NAME=company.name, COMPANY_ABBR=abbr)
		before = (frappe.db.count("Account"), frappe.db.count("Letter Head"), frappe.db.count("POS Profile"))
		with self.assertRaises(frappe.ValidationError) as ctx:
			store_setup.run(cfg)
		self.assertIn(f"Write Off - {abbr}", str(ctx.exception))
		self.assertIn("write_off_account", str(ctx.exception))
		self.assertEqual((frappe.db.count("Account"), frappe.db.count("Letter Head"), frappe.db.count("POS Profile")), before,
			"a missing account must refuse before anything is written")
		self.assertFalse(frappe.db.exists("Account", f"Sales Tax Payable - {abbr}"))

	def test_cash_drawer_accounts_are_made_without_a_migrate(self):
		for acct in ("Cash Over/Short", "Meals"):
			if frappe.db.exists("Account", f"{acct} - {self.abbr}"):
				frappe.delete_doc("Account", f"{acct} - {self.abbr}", force=True, ignore_permissions=True)
		_, log = _run(_config_no_mail())
		for acct in ("Cash Over/Short", "Meals"):
			self.assertEqual(frappe.db.get_value("Account", f"{acct} - {self.abbr}", "parent_account"),
				f"Indirect Expenses - {self.abbr}")
		self.assertIn(f"account created Cash Over/Short - {self.abbr}", log)
		_, log = _run(_config_no_mail())
		self.assertNotIn("account created", log)

	def test_standard_chart_parents(self):
		_run(_config_no_mail())
		self.assertEqual(frappe.db.get_value("Account", f"Sales Tax Payable - {self.abbr}", "parent_account"),
			f"Duties and Taxes - {self.abbr}")
		self.assertEqual(frappe.db.get_value("Account", f"Card Clearing - {self.abbr}", "parent_account"),
			f"Bank Accounts - {self.abbr}")

	def test_4473_mode_blank_changes_nothing(self):
		frappe.db.set_single_value("FFL Settings", "paper_4473_mode", 0)
		frappe.db.set_single_value("FastBound Settings", "enabled", 1)
		_, log = _run(_config_no_mail())
		self.assertEqual(frappe.db.get_single_value("FFL Settings", "paper_4473_mode"), 0)
		self.assertEqual(frappe.db.get_single_value("FastBound Settings", "enabled"), 1)
		self.assertNotIn("paper_4473_mode", log)

	def test_4473_mode_paper_then_fastbound(self):
		from ffl_core.compliance.mode import paper_4473_mode
		_run(_config_no_mail(FORM_4473_MODE="Paper"))
		self.assertTrue(paper_4473_mode())
		self.assertEqual(frappe.db.get_single_value("FastBound Settings", "enabled"), 0)
		_, log = _run(_config_no_mail(FORM_4473_MODE="paper"))
		self.assertNotIn("paper_4473_mode", log, "a second run must not rewrite it")
		_run(_config_no_mail(FORM_4473_MODE="fastbound"))
		self.assertFalse(paper_4473_mode())
		self.assertEqual(frappe.db.get_single_value("FastBound Settings", "enabled"), 1)

	def test_unknown_4473_mode_is_refused_before_anything_is_written(self):
		before = frappe.db.count("Account")
		with self.assertRaises(frappe.ValidationError):
			store_setup.run(_config_no_mail(FORM_4473_MODE="off"))
		self.assertEqual(frappe.db.count("Account"), before)


class StoreSetupNumberedChart(FrappeTestCase):
	"""ERPNext's other standard chart names every account "<number> - <name> - <abbr>"; the setup used to
	look its parents up by the unnumbered name and crash ("Could not find Parent Account")."""

	ABBR = "TNC"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.company = frappe.get_doc({
			"doctype": "Company", "company_name": "_Test Numbered Chart Co", "abbr": cls.ABBR,
			"default_currency": "USD", "country": "United States",
			"create_chart_of_accounts_based_on": "Standard Template",
			"chart_of_accounts": "Standard with Numbers",
		}).insert(ignore_permissions=True)
		# Other companies' POS Profiles are not under test, and the Payroc step saves every enabled profile, which
		# fails for one whose company has no Credit Card account (rolled back with the class).
		frappe.db.set_value("POS Profile", {"company": ("!=", cls.company.name)}, "disabled", 1)
		cls.cfg = _config_no_mail(COMPANY_NAME=cls.company.name, COMPANY_ABBR=cls.ABBR, CFET_RATE="11",
			SALES_TAX_TITLE="_Test Numbered Tax", ITT_TITLE="_Test Numbered Goods")
		cls.result, cls.log = _run(cls.cfg)

	def _parent(self, name):
		return frappe.db.get_value("Account", f"{name} - {self.ABBR}", "parent_account")

	def test_chart_really_is_the_numbered_one(self):
		self.assertFalse(frappe.db.exists("Account", f"Duties and Taxes - {self.ABBR}"))
		self.assertTrue(frappe.db.exists("Account", f"2300 - Duties and Taxes - {self.ABBR}"))

	def test_new_accounts_hang_under_the_numbered_groups(self):
		self.assertEqual(self._parent("Sales Tax Payable"), f"2300 - Duties and Taxes - {self.ABBR}")
		self.assertEqual(self._parent("CA Firearm Excise Tax Payable"), f"2300 - Duties and Taxes - {self.ABBR}")
		self.assertEqual(self._parent("Bank Account"), f"1200 - Bank Accounts - {self.ABBR}")
		self.assertEqual(self._parent("Card Clearing"), f"1200 - Bank Accounts - {self.ABBR}")

	def test_cash_drawer_accounts_hang_under_indirect_expenses(self):
		for acct in ("Cash Over/Short", "Meals"):
			self.assertEqual(self._parent(acct), f"5200 - Indirect Expenses - {self.ABBR}")

	def test_pos_profile_and_cash_tender_use_the_companys_own_accounts(self):
		p = frappe.db.get_value("POS Profile", self.ABBR,
			["income_account", "expense_account", "write_off_account"], as_dict=True)
		co = frappe.db.get_value("Company", self.company.name,
			["default_income_account", "default_expense_account", "write_off_account", "default_cash_account"],
			as_dict=True)
		self.assertEqual((p.income_account, p.expense_account, p.write_off_account),
			(co.default_income_account, co.default_expense_account, co.write_off_account))
		self.assertEqual(frappe.db.get_value("Mode of Payment Account",
			{"parent": "Cash", "company": self.company.name}, "default_account"), co.default_cash_account)

	def test_tax_math_is_right_on_the_numbered_chart(self):
		pistol = self.result["verify"]["Pistol"]
		self.assertEqual(pistol["sales_tax"], 82.5)
		self.assertEqual(pistol["cfet"], 110.0)

	def test_a_settled_site_is_written_nothing(self):
		# The first re-run may put the exemption template's two rows in store_setup's order (tax.ensure
		# made them in its own); that is not chart-specific. The run after that must change nothing.
		_run(self.cfg)
		_, log = _run(self.cfg)
		noise = [l for l in log.splitlines() if l.startswith("STORE_SETUP")
			and not any(k in l for k in ("left alone", "tax.ensure", "SMTP not configured", "COMMITTED"))]
		self.assertEqual(noise, [])


class StoreSetupRefuses(FrappeTestCase):
	def test_missing_required_fact_throws_before_writing(self):
		before = (frappe.db.count("Letter Head"), frappe.db.count("Account"),
			frappe.db.get_value("Letter Head", {"is_default": 1}, ["name", "modified"]))
		with self.assertRaises(frappe.ValidationError):
			store_setup.run(_config(FFL_NUMBER=""))
		after = (frappe.db.count("Letter Head"), frappe.db.count("Account"),
			frappe.db.get_value("Letter Head", {"is_default": 1}, ["name", "modified"]))
		self.assertEqual(after, before, "refusal must write nothing")

	def test_wrong_abbr_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			store_setup.run(_config(COMPANY_ABBR="NOPE"))

	def test_old_image_without_receipt_fields_is_refused(self):
		real = frappe.get_meta

		class _NoFooter:
			def __init__(self, meta):
				self._m = meta

			def has_field(self, f):
				return False if f == "receipt_footer_note" else self._m.has_field(f)

			def __getattr__(self, k):
				return getattr(self._m, k)

		def fake(dt, *a, **k):
			m = real(dt, *a, **k)
			return _NoFooter(m) if dt == "FFL Settings" else m

		with patch("ffl_core.setup.store_setup.frappe.get_meta", side_effect=fake):
			with self.assertRaises(frappe.ValidationError):
				store_setup.run(_config())

	def test_letter_head_html_escapes_values_and_omits_blank_lines(self):
		c = store_setup._normalize(_config(STORE_PHONE="<b>555</b>", CFD_NUMBER="", LOGO_URL=""))
		html = store_setup.letter_head_html(c)
		self.assertIn("&lt;b&gt;555&lt;/b&gt;", html)
		self.assertNotIn("<b>555", html)
		self.assertNotIn("CFD#", html)
		self.assertNotIn("<img", html)


class StoreEnvExample(FrappeTestCase):
	"""deploy/prod/store.env.example is what a newcomer copies: it must list exactly the keys store_setup reads."""

	EXAMPLE = Path(__file__).resolve().parents[4] / "deploy" / "prod" / "store.env.example"

	def test_the_example_lists_every_key_once_and_nothing_else(self):
		if not self.EXAMPLE.exists():
			self.skipTest("deploy/prod is not part of this checkout")
		keys = re.findall(r"^([A-Z][A-Z0-9_]*)=", self.EXAMPLE.read_text(), re.M)
		self.assertEqual(sorted(keys), sorted(store_setup.KEYS))
		self.assertEqual(len(keys), len(set(keys)))

	def test_the_example_values_are_not_a_real_store(self):
		if not self.EXAMPLE.exists():
			self.skipTest("deploy/prod is not part of this checkout")
		values = dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", self.EXAMPLE.read_text(), re.M))
		self.assertEqual(values["FFL_NUMBER"], "9-99-999-99-9A-99999")
		self.assertTrue(values["OWNER_EMAIL"].endswith("@example.com"))
		self.assertEqual(values["SMTP_PASSWORD"], "")
