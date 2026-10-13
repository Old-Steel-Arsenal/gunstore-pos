"""Create Order Draft — a half-edited Create Order page, parked for later.

Stores the page's form state verbatim as JSON (see api/manual_order.py
save_order_draft / get_order_draft). Deliberately shared shop-wide (role
permissions, no owner restriction) so one staffer can park an order and
another can finish it. Serials in a draft are NOT reserved — the page
re-validates every firearm when the draft is resumed.
"""
from __future__ import annotations

from frappe.model.document import Document


class CreateOrderDraft(Document):
	pass
