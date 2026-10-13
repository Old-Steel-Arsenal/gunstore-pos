"""Distributor Markup Rule (child of Distributor). Pricing engine reads these;
Department OR brand scope,
pct and/or flat."""
from __future__ import annotations

from frappe.model.document import Document


class DistributorMarkupRule(Document):
	pass
