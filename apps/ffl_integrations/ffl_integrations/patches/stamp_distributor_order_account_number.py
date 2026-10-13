"""Distributor Order.account_number — the house's own account identifier, shown in
place of the lane key. Backfill existing orders from their provider's settings."""
import frappe


def execute():
	frappe.reload_doc("ffl_integrations", "doctype", "distributor_order")
	from ffl_integrations.distributor.api import get_provider

	cache: dict = {}
	for row in frappe.get_all("Distributor Order", fields=["name", "distributor", "account"],
			filters={"account_number": ("in", ["", None])}):
		try:
			provider = cache.setdefault(row.distributor,
				get_provider(frappe.get_cached_doc("Distributor", row.distributor).provider))
			number = provider.account_number(row.account or "")
		except Exception:  # noqa: BLE001 — a display stamp; skip what cannot be read
			continue
		if number:
			frappe.db.set_value("Distributor Order", row.name, "account_number", number,
				update_modified=False)
