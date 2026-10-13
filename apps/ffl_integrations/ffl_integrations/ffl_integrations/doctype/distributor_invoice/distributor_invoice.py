"""Distributor Invoice — a read-only mirror of one distributor invoice, pulled by
``distributor.invoices.sync_invoices`` for every order on the account (placed
from this POS or not). Display only: it never posts to the books."""
from __future__ import annotations

from frappe.model.document import Document


class DistributorInvoice(Document):
	pass
