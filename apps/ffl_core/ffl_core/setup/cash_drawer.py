"""Cash drawer setup: two fields on the POS Closing Entry, one on the Purchase Receipt, and the accounts every
company needs.

Run at after_install (fields only: a fresh site has no company yet), at after_migrate and from the
patch, so a site that predates the feature, a fresh install and a store that adds a company later all
end up the same. Idempotent; never raises (a thrown after_migrate hook aborts the whole migrate): a
failure is logged and the next run retries. Accounts are created per company from its abbr
(``Cash Over/Short - <ABBR>``, ``Operating Bank - <ABBR>`` ...), nothing is
hard-coded to one store.
"""
from __future__ import annotations

import frappe
from frappe.utils import now_datetime
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ffl_core.utils import commit

_FIELDS = {
	"POS Closing Entry": [
		{
			"fieldname": "ffl_cash_counted", "fieldtype": "Check", "label": "Cash Counted", "read_only": 1,
			"insert_after": "payment_reconciliation",
			"description": "Ticked when someone counted the drawer; otherwise the Cash row simply repeats the books.",
		},
		{
			"fieldname": "ffl_cash_drawer_close", "fieldtype": "Link", "options": "Cash Drawer Close",
			"label": "Cash Drawer Close", "read_only": 1, "insert_after": "ffl_cash_counted",
		},
	],
	# A private seller paid outside the system (a hand journal entry, a check booked in QuickBooks): set and cleared
	# from the Cash Drawer page (cash_drawer.mark_paid_outside); who and when are in the receipt's comments. A duplicate
	# does not copy it; an amendment copies it (Desk amends no_copy fields too) and answers to its original's mark anyway
	# (cash_drawer.receipt_chain).
	# The Cash Drawer Entry or Cash Drawer Close a journal entry books, stamped when the drawer posts it
	# (cash_drawer._post_je): such an entry is cancelled only from the Cash Drawer page (refuse_drawer_je_cancel).
	"Journal Entry": [
		{
			"fieldname": "ffl_cash_drawer_source", "fieldtype": "Data", "label": "Cash Drawer Source",
			"read_only": 1, "no_copy": 1, "insert_after": "user_remark",
		},
	],
	"Purchase Receipt": [
		{
			"fieldname": "ffl_seller_paid_outside", "fieldtype": "Small Text", "label": "Seller Paid Outside the System",
			"read_only": 1, "allow_on_submit": 1, "no_copy": 1, "insert_after": "ffl_seller_customer",
			"depends_on": "eval:doc.ffl_seller_paid_outside",
			"description": "How the private seller was paid outside the system. No seller payment is recorded for this "
				"receipt while it is set; who set it and when is in the comments.",
		},
	],
}


def ensure_fields() -> None:
	create_custom_fields(_FIELDS, ignore_validate=True)


def ensure_tracking_started() -> None:
	"""Remember when cash tracking went live (once): a purchase from before it is the accountant's, not a payout
	to record afterwards."""
	if not frappe.db.sql("SELECT 1 FROM `tabSingles` WHERE doctype = 'Cash Drawer Settings' AND field = 'tracking_started'"):
		frappe.db.set_single_value("Cash Drawer Settings", "tracking_started", now_datetime())


def ensure_accounts() -> list[str]:
	"""Accounts + default bank account for every company. Returns the companies done."""
	from ffl_core.cash_drawer import ensure_accounts as ensure_company

	done = []
	for company in frappe.get_all("Company", pluck="name"):
		try:
			# No bank account is made here: right after a migrate on a new site store_setup has not run yet.
			ensure_company(company, create_bank=False)
			done.append(company)
		except frappe.ValidationError:
			# A company with an unusual chart of accounts: say so, keep going.
			frappe.log_error(title=f"cash drawer: accounts for {company}", message=frappe.get_traceback())
	return done


def ensure_fields_only() -> None:
	"""after_install: the fields (a fresh site has no company yet)."""
	try:
		ensure_fields()
		ensure_tracking_started()
		commit()
	except Exception:
		frappe.log_error(title="cash_drawer.ensure_fields failed", message=frappe.get_traceback())


def ensure() -> dict:
	"""after_migrate and the patch: fields and accounts."""
	try:
		ensure_fields()
		ensure_tracking_started()
		done = ensure_accounts()
		commit()
		return {"ok": 1, "companies": done}
	except Exception:
		frappe.log_error(title="cash_drawer.ensure failed", message=frappe.get_traceback())
		return {"error": 1}
