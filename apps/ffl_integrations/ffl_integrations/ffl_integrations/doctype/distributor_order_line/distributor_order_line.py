"""Distributor Order Line (child of Distributor Order). One SKU on an order;
carries the wish/ack/invoiced/received quantities and the RSR line echoes
(serials, tracking, invoices) the poller writes back."""
from __future__ import annotations

from frappe.model.document import Document


class DistributorOrderLine(Document):
	pass
