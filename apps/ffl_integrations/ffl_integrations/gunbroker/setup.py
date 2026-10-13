"""The accounts and payment modes a GunBroker sale posts through.

Seven accounts, and the reason there are seven rather than "put it all in
sundry" is that four different people are owed four different explanations at
month end.

*Two collected-for-someone-else liabilities.* GunBroker is the marketplace
facilitator: it computes the sales tax, adds it to the buyer's total, and files
it with the state — but the money travels buyer → seller → GunBroker, so it
lands in our bank and sits as a liability until GunBroker's monthly statement
asks for it. The buyer's 1% service fee rides in the same ``salesTaxTotal``
figure and is a different liability owed to a different party, which is why the
two are split rather than netted. Neither may touch ``Sales Tax Payable``: that
account is what the Texas return is built from, and a marketplace sale belongs
in Total Sales and NOT in Taxable Sales.

*Two accrual accounts for what GunBroker charges us.* The final value fee and
the licence document fee are billed on the 2nd and taken from the bank on the
20th, so the expense belongs to the month of the sale and the payment to the
month it clears.

*Three loss accounts* for the ways a sale comes apart: a chargeback, a bad
cheque, and — separately from both — the bank's own fee for handling one. The
first two do not exist in this company's chart of accounts and are created
here rather than left to somebody improvising at year end. The third one does
exist (``Bank Charges``) and is deliberately reused.

The liability accounts are created with NO ``account_type``, on purpose. Payable
would make ERPNext demand a party on every journal entry against them, and Tax
would sweep them into any logic that finds tax accounts by type — including the
Sales Report's own fail-closed classifier, which exists precisely so an
unexpected account shows up as unclassified instead of quietly counted as
collected sales tax.

The two Modes of Payment matter for a reason that is easy to miss: a Payment
Entry's bank account comes from the MODE, not from anything on the order. So
the Settings links have to be written into the modes' company rows, or changing
the setting is theatre and receipts keep landing in the old account.
"""
from __future__ import annotations

import frappe
from frappe import _

SETTINGS_DOCTYPE = "GunBroker Settings"

MODE_CHECK = "GunBroker Check"
MODE_CARD = "GunBroker Card"

CLEARING_ACCOUNT = "GunBroker Clearing"
MPF_TAX_ACCOUNT = "GunBroker MPF Sales Tax Payable"
MSF_ACCOUNT = "GunBroker Marketplace Fee Payable"
ACCRUED_FEES_ACCOUNT = "Accrued GunBroker Fees"
FEES_EXPENSE_ACCOUNT = "Marketplace Fees - GunBroker"
CHARGEBACK_ACCOUNT = "Chargeback Losses"
BAD_DEBT_ACCOUNT = "Bad Debts Written Off"

# Already in this company's chart of accounts — reused, never re-created. A
# returned-cheque fee is an ordinary bank charge and does not want a special
# account.
BANK_CHARGES_ACCOUNT = "Bank Charges"


def ensure(company: str | None = None) -> None:
	"""Create the accounts and payment modes. Idempotent; safe on every migrate.

	Each company is wrapped on its own: a chart of accounts we cannot find a
	home in must not stop the rest of the migrate, and the failure is worth an
	Error Log rather than a broken deploy.
	"""
	companies = [company] if company else frappe.get_all("Company", pluck="name")
	for name in companies:
		try:
			ensure_accounts(name)
			ensure_modes_of_payment(name)
		except Exception:  # noqa: BLE001 — a seed must not break bench migrate
			frappe.log_error(
				title=f"GunBroker account seed failed for {name}",
				message=frappe.get_traceback())


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def ensure_accounts(company: str) -> dict[str, str]:
	"""All seven, by role. Returns {role: account name}."""
	abbr = frappe.get_cached_value("Company", company, "abbr")
	liability = _parent(company, abbr, "Liability",
		("Current Liabilities", "Accounts Payable"))
	expense = _parent(company, abbr, "Expense",
		("Indirect Expenses", "Expenses"))
	out = {
		"mpf_tax": _ensure_account(company, MPF_TAX_ACCOUNT, liability),
		"msf": _ensure_account(company, MSF_ACCOUNT, liability),
		"accrued_fees": _ensure_account(company, ACCRUED_FEES_ACCOUNT, liability),
		"fees_expense": _ensure_account(company, FEES_EXPENSE_ACCOUNT, expense,
			account_type="Expense Account"),
		"chargeback": _ensure_account(company, CHARGEBACK_ACCOUNT, expense,
			account_type="Expense Account"),
		"bad_debt": _ensure_account(company, BAD_DEBT_ACCOUNT, expense,
			account_type="Expense Account"),
	}
	clearing = ensure_clearing_account(company)
	if clearing:
		out["clearing"] = clearing
	return out


def ensure_clearing_account(company: str) -> str:
	"""The card clearing account, created even though this phase takes no cards.

	Not created when Settings already names one. That is the rule and
	it is the right way round: a store that has pointed GunBroker card money at
	its existing Payroc clearing account has said where it wants that money to
	land, and adding a second, empty, near-identically named Bank account
	underneath the same parent gives the month-end reconciliation two places to
	look and one of them always zero.

	Where nothing is named, building it now costs nothing and building it in a
	hurry the day a gateway is connected is how a receipt ends up in the wrong
	account. It goes next to the existing card clearing account, so the
	reconciliation story stays one story.
	"""
	configured = (frappe.db.get_single_value(SETTINGS_DOCTYPE,
		"card_clearing_account") or "").strip()
	if configured:
		return configured
	abbr = frappe.get_cached_value("Company", company, "abbr")
	name = f"{CLEARING_ACCOUNT} - {abbr}"
	if frappe.db.exists("Account", name):
		return name
	parent = (frappe.db.get_value("Account", f"Payroc Card Clearing - {abbr}",
		"parent_account") or f"Bank Accounts - {abbr}")
	if not frappe.db.exists("Account", parent):
		parent = _parent(company, abbr, "Asset", ("Bank Accounts", "Current Assets"))
	return _ensure_account(company, CLEARING_ACCOUNT, parent, account_type="Bank")


def _ensure_account(company: str, account_name: str, parent: str,
		account_type: str | None = None) -> str:
	abbr = frappe.get_cached_value("Company", company, "abbr")
	name = f"{account_name} - {abbr}"
	if frappe.db.exists("Account", name):
		return name
	doc = frappe.get_doc({
		"doctype": "Account",
		"account_name": account_name,
		"company": company,
		"parent_account": parent,
		"is_group": 0,
		**({"account_type": account_type} if account_type else {}),
	})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _parent(company: str, abbr: str, root_type: str,
		preferred: tuple[str, ...]) -> str:
	"""A group account of the right root type, by preferred name then by tree."""
	for candidate in preferred:
		name = f"{candidate} - {abbr}"
		if frappe.db.exists("Account",
				{"name": name, "company": company, "is_group": 1}):
			return name
	group = frappe.db.get_value(
		"Account", {"company": company, "root_type": root_type, "is_group": 1},
		"name", order_by="lft")
	if not group:
		frappe.throw(_("No {0} parent account exists for {1}.").format(
			root_type, company))
	return group


# ---------------------------------------------------------------------------
# Modes of payment
# ---------------------------------------------------------------------------

def ensure_modes_of_payment(company: str) -> dict[str, str]:
	"""The two GunBroker tenders, each pointed at its configured account.

	Re-pointed on every run, not only on creation. The account a receipt lands
	in is read off the mode, so an operator who changes
	``check_deposit_account`` in Settings and sees nothing change would be
	right: without this the setting is decorative and every cheque keeps
	posting to whatever the mode said the day it was made.
	"""
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	check_account = (settings.check_deposit_account or "").strip()
	card_account = (settings.card_clearing_account or "").strip() \
		or ensure_clearing_account(company)
	out = {}
	if check_account:
		out[MODE_CHECK] = _ensure_mode(MODE_CHECK, company, check_account)
	if card_account:
		out[MODE_CARD] = _ensure_mode(MODE_CARD, company, card_account)
	return out


def _ensure_mode(mode: str, company: str, account: str) -> str:
	if not frappe.db.exists("Mode of Payment", mode):
		doc = frappe.get_doc({
			"doctype": "Mode of Payment",
			"mode_of_payment": mode,
			"type": "Bank",
			"enabled": 1,
			"accounts": [{"company": company, "default_account": account}],
		})
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc.name
	doc = frappe.get_doc("Mode of Payment", mode)
	row = next((r for r in (doc.accounts or []) if r.company == company), None)
	if row is None:
		doc.append("accounts", {"company": company, "default_account": account})
	elif row.default_account != account:
		row.default_account = account
	else:
		return mode
	doc.flags.ignore_permissions = True
	doc.save()
	return mode


# ---------------------------------------------------------------------------
# Names for the posting code
# ---------------------------------------------------------------------------

def account_name(base: str, company: str) -> str:
	"""``"Accrued GunBroker Fees"`` → ``"Accrued GunBroker Fees - <ABBR>"``.

	Derived from the abbreviation without touching the database, because the
	invoice builder calls it several times per posting.
	"""
	abbr = frappe.get_cached_value("Company", company, "abbr")
	return f"{base} - {abbr}"
