# Copyright (c) 2026, GunStore POS and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class WooOnlineOrderRefund(Document):
	"""One recorded WooCommerce refund on a Woo Online Order (child table).

	Deduped by ``woo_refund_id`` — the idempotency key that keeps a re-delivered
	``order.updated`` webhook from booking the same partial refund twice.
	"""

	pass
