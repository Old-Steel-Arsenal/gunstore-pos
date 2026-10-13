"""Back-fill: submit Retail Sale dispositions the FastBound webhook left in Draft.

Before the webhook learned to submit them, every completed counter sale's FFL
Disposition was inserted in Draft and never committed to the bound book. A
completed 4473 + NICS Proceed is a finished, legal sale, so it belongs as a
submitted A&D record. This commits the historical drafts.

Idempotent (only docstatus=0 Completed/Proceed Retail Sales match). Submitting
is a no-op beyond committing the record: a Retail Sale skips the stock-out (the
POS Invoice already moved the gun) and the FastBound push is skipped because
fastbound_id is set. Each is committed individually so one bad row (e.g. a
deleted serial) is logged and skipped without blocking the rest.
"""
import frappe


def execute():
	names = frappe.get_all(
		"FFL Disposition",
		filters={
			"docstatus": 0,
			"disposition_type": "Retail Sale",
			"form_4473_status": "Completed",
			"nics_status": "Proceed",
		},
		pluck="name",
	)
	for name in names:
		try:
			doc = frappe.get_doc("FFL Disposition", name)
			doc.flags.ignore_permissions = True
			doc.submit()
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title="submit_completed_retail_sale_dispositions failed",
				message=f"{name}\n\n{frappe.get_traceback()}")
