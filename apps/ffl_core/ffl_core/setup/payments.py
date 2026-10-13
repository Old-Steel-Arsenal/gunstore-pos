"""Manual (no-integration) Modes of Payment: Zelle + ACH.

Both settle to the bank, like a wire — they carry no external API (unlike
Payroc card processing), so they're pure-manual tender that lives in ffl_core.
ensure_manual_modes_of_payment creates each Mode of Payment, wires a per-company
GL (bank, falling back to cash) account so an ``is_pos`` Sales Invoice can clear
its payment, and adds the tile to every enabled POS Profile.

Hooked on after_migrate (master data — accounts/companies/POS profiles — must
exist first). Idempotent and self-contained: any failure is logged, never
raised, so it can't break a migrate.
"""
from __future__ import annotations

import frappe
from ffl_core.utils import commit

ZELLE = "Zelle"
MANUAL_MODES = (ZELLE, "ACH")


def ensure_manual_modes_of_payment() -> None:
	try:
		for name in MANUAL_MODES:
			_ensure_mode_with_accounts(name)
		_wire_into_pos_profiles()
		commit()
	except Exception:
		frappe.log_error(
			title="ffl_core.setup.payments: ensure failed",
			message=frappe.get_traceback())


def _ensure_mode_with_accounts(name: str) -> None:
	if not frappe.db.exists("Mode of Payment", name):
		doc = frappe.get_doc({
			"doctype": "Mode of Payment",
			"mode_of_payment": name,
			"type": "Bank",  # settles to the bank, not the cash drawer
			"enabled": 1,
		})
		doc.flags.ignore_permissions = True
		doc.insert()

	mop = frappe.get_doc("Mode of Payment", name)
	existing = {row.company: row for row in (mop.accounts or [])}
	changed = False
	for company in frappe.get_all("Company", pluck="name"):
		account = _company_bank_account(company)
		row = existing.get(company)
		if row is None:
			if account:
				mop.append("accounts", {"company": company, "default_account": account})
				changed = True
		elif row.default_account and not _is_asset_account(row.default_account):
			# Self-heal a previously mis-resolved P&L account (e.g. an earlier
			# build picked "Bank Charges" by name) — a payment account must be an
			# asset, so re-point it. Honours an admin's valid asset choice. (Don't
			# let a missing asset account for THIS company skip the heal — flag it.)
			if account:
				row.default_account = account
				changed = True
			else:
				frappe.log_error(
					title="ffl_core.setup.payments: cannot heal mode-of-payment account",
					message=f"{name} / {company}: default_account {row.default_account!r} "
						"is not an asset account and no Bank/Cash asset account was found "
						"to re-point it. is_pos payments via this mode will fail until a "
						"valid account is set.")
	if changed:
		mop.flags.ignore_permissions = True
		mop.save()


def _wire_into_pos_profiles() -> None:
	for pname in frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name"):
		profile = frappe.get_doc("POS Profile", pname)
		existing = {row.mode_of_payment for row in (profile.payments or [])}
		added = False
		for name in MANUAL_MODES:
			if name in existing:
				continue
			profile.append("payments", {"mode_of_payment": name, "default": 0})
			added = True
		if added:
			profile.flags.ignore_permissions = True
			profile.save()


def _company_bank_account(company: str) -> str | None:
	"""A posting *asset* account (Bank, else Cash) for the company so an is_pos
	payment GL clears. Zelle/ACH settle to the bank. Every candidate is
	constrained to root_type='Asset' so a same-named P&L account (e.g. "Bank
	Charges", an Expense) can never be picked — posting a tender to a P&L
	account fails at submit ("Cost Center required for P&L account")."""
	acct = frappe.db.get_value("Company", company, "default_bank_account")
	if acct:
		return acct
	for filters in (
		{"company": company, "account_type": "Bank", "is_group": 0, "root_type": "Asset"},
		{"company": company, "account_type": "Cash", "is_group": 0, "root_type": "Asset"},
		{"company": company, "account_name": ("like", "Bank%"), "is_group": 0, "root_type": "Asset"},
		{"company": company, "account_name": ("like", "Cash%"), "is_group": 0, "root_type": "Asset"},
	):
		row = frappe.get_all("Account", filters=filters, pluck="name", limit=1)
		if row:
			return row[0]
	return None


def _is_asset_account(account: str) -> bool:
	return frappe.db.get_value("Account", account, "root_type") == "Asset"
