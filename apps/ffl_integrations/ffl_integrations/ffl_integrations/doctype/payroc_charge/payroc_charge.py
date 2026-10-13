"""Payroc Charge (child of POS Invoice, `payroc_charges`). One row per device
payment instruction — a POS sale split across several physical cards carries
one row per card. The legacy single-value payroc_* fields on POS Invoice are
maintained as aggregates of these rows (see payroc.api._refresh_charge_stamps);
invoices from before this table exist have stamps but no rows, and every
lifecycle path falls back to the legacy single-charge behavior for them."""
from __future__ import annotations

from frappe.model.document import Document


class PayrocCharge(Document):
	pass
