"""Stamp every journal entry the cash drawer already made with what it books (Journal Entry.ffl_cash_drawer_source):
a Cash Drawer Entry's, and a count's variance / cut-off / true-up. Entries posted from now on are stamped when they
are posted (cash_drawer._post_je). Idempotent."""
import frappe

from ffl_core.setup.cash_drawer import ensure_fields


def execute():
	ensure_fields()  # the field comes from after_migrate, which runs after the patches
	frappe.db.sql("""UPDATE `tabJournal Entry` je JOIN `tabCash Drawer Entry` e ON e.journal_entry = je.name
		SET je.ffl_cash_drawer_source = e.name WHERE IFNULL(je.ffl_cash_drawer_source, '') = ''""")
	for field in ("variance_je", "cutoff_je", "trueup_je"):
		frappe.db.sql(f"""UPDATE `tabJournal Entry` je JOIN `tabCash Drawer Close` c ON c.{field} = je.name
			SET je.ffl_cash_drawer_source = c.name WHERE IFNULL(je.ffl_cash_drawer_source, '') = ''""")
