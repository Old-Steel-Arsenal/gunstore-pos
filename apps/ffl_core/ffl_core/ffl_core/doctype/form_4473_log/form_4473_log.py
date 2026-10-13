"""Form 4473 Log — the durable record of every completed ATF Form 4473.

One row per 4473: written by the form4473.completed webhook (FastBound 4473s,
whatever the NICS outcome, matched to a POS order or not) and by the paper /
manager-override path (ffl_integrations.fastbound.pos_4473). The POS Invoice a
4473 ran on is NOT a durable home for it: a draft can be deleted (Clear
Abandoned Order, a Denied buyer's draft) and Redo 4473 wipes its link — so the
order is stored here as plain text and the row itself can never be deleted.
The signed PDF is copied in as a private attachment
(ffl_integrations.fastbound.pdf_archive).
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class Form4473Log(Document):
	def on_trash(self):
		frappe.throw(_("{0} is a 4473 record — it cannot be deleted.").format(self.name))


def record_4473(*, form4473_id: str | None = None, pos_invoices=(), customer: str | None = None,
		serials=(), nics_status: str = "", source: str = "FastBound",
		fastbound_disposition_id: str = "", pdf_url: str = "", completed_on=None) -> str:
	"""Create — or, for a FastBound 4473 already on file, update — its log row.

	Keyed by ``form4473_id`` when there is one, so a redelivered webhook updates
	the row instead of adding a second. An update only ever ADDS what it knows:
	a blank field (a redelivery that matched no draft) never erases a value.
	Paper / override 4473s have no id and always get a new row. Returns the name.
	"""
	name = frappe.db.get_value("Form 4473 Log", {"form4473_id": form4473_id}) if form4473_id else None
	doc = frappe.get_doc("Form 4473 Log", name) if name else frappe.new_doc("Form 4473 Log")
	invoices = [i for i in (doc.pos_invoice or "").split(", ") if i]
	invoices += [i for i in pos_invoices if i and i not in invoices]
	serial_list = [s for s in (doc.serials or "").split(", ") if s]
	serial_list += [s for s in serials if s and s not in serial_list]
	doc.update({
		# None, never "": the column is unique and paper rows have no id.
		"form4473_id": form4473_id or doc.form4473_id or None,
		"pos_invoice": ", ".join(invoices),
		"serials": ", ".join(serial_list),
		"source": source,
		"nics_status": nics_status or doc.nics_status,
		"fastbound_disposition_id": fastbound_disposition_id or doc.fastbound_disposition_id,
		"pdf_url": pdf_url or doc.pdf_url,
	})
	if customer and not doc.customer:
		doc.customer = customer
		doc.customer_name = frappe.db.get_value("Customer", customer, "customer_name") or customer
	if doc.is_new():
		doc.completed_on = completed_on or now_datetime()
		doc.pdf_status = "Pending" if doc.form4473_id else "No PDF"
	doc.flags.ignore_permissions = True
	# A record, not a transaction: a customer deleted since must not block it.
	doc.flags.ignore_links = True
	doc.save()
	return doc.name
