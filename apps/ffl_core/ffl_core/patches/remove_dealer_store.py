"""The dealer (wholesale) WooCommerce store is retired; the code is
gone, and this removes what it left on a site.

In this order:

1. Two fields that stay are moved off the anchors that are about to go, so the forms keep their
   layout: the Serial No GunBroker section (after the dealer store section → after the retail
   WooCommerce section's last field) and the Item's Listing Status (inside the "Dealer Portal"
   section → straight under the Channels tab). The fixtures say the same, so the fixture sync
   that runs after this patch writes the value it already has.
2. The 17 custom fields: the dealer store sections on Item and Serial No, the Item's "Dealer
   Portal" section with its publish flag, and the Customer's dealer credit section and limit.
3. What the Dealer WooCommerce Settings doctype left elsewhere: its saved values and stored
   passwords, its Property Setters and Custom Fields, its custom permission rows, shares, and
   any desk Workspace link / shortcut / sidebar row that opens it.
4. The Dealer Portal switch's saved value on FFL Settings.
5. A safety net, not a migration: neither shop ever took an order on the dealer store (owner,
   2026-10-09), and 1.11.0 drops "dealer" from Woo Online Order's ``site``. Should a
   ``site='dealer'`` order exist anyway, one Error Log lists it for a human; the order and
   everything linked to it are left exactly as they are.

The DocType row itself is NOT deleted here: ``bench migrate`` runs
``frappe.model.sync.remove_orphan_doctypes`` after the post_model_sync patches (frappe/migrate.py,
``post_schema_updates``), and that deletes every standard DocType whose controller is gone — this
one, since 1.11.0 ships without it. Deleting a DocType removes neither its Singles nor its
``__Auth`` rows nor the rows above, which is why this patch does.

Not touched: the DB columns (frappe never drops them for a deleted custom field; dropping is a
later, manual ``bench trim-tables``), and every field that only shares the word: Item.dealer_price
(the "Cost" field), Serial No.dealer_sell_price, Purchase Receipt Item.ffl_dealer_price and
Item.listing_status keep their definitions and data.

Idempotent: on a second run, or on a site that never had any of it, nothing is found.
"""
from __future__ import annotations

import frappe

SETTINGS_DOCTYPE = "Dealer WooCommerce Settings"

# Custom Field name -> the anchor it keeps after the dealer fields are gone.
REANCHOR = {
	"Serial No-sn_section_gunbroker": "woo_sold_archived",
	"Item-listing_status": "channels_tab",
}

CUSTOM_FIELDS = (
	"Item-item_section_dealer_woo",
	"Item-dealer_woo_product_id",
	"Item-dealer_woo_listed",
	"Item-dealer_woo_synced_at",
	"Item-dealer_woo_sync_error",
	"Item-dealer_woo_delisted",
	"Serial No-sn_section_dealer_woo",
	"Serial No-dealer_woo_product_id",
	"Serial No-dealer_woo_synced_at",
	"Serial No-dealer_woo_sync_error",
	"Serial No-dealer_woo_delisted",
	"Serial No-dealer_woo_sold_archived",
	"Item-section_listing",
	"Item-publish_to_portal",
	"Item-column_listing_a",
	"Customer-section_ffl_credit",
	"Customer-dealer_credit_limit",
)

# FFL Settings fields that left with the switch (the tab and section store nothing, but a stray
# row is removed all the same).
FFL_SETTINGS_FIELDS = ("tab_subsystems", "section_dealer_portal", "dealer_portal_enabled")


def execute() -> None:
	for name, anchor in REANCHOR.items():
		if frappe.db.exists("Custom Field", name):
			# A raw write: a Custom Field save would re-run the doctype's schema sync.
			frappe.db.set_value("Custom Field", name, "insert_after", anchor, update_modified=False)

	doctypes = {name.split("-", 1)[0] for name in (*REANCHOR, *CUSTOM_FIELDS)}
	for name in CUSTOM_FIELDS:
		frappe.delete_doc_if_exists("Custom Field", name)

	_remove_settings_doctype_leftovers()
	frappe.db.delete("Singles", {"doctype": "FFL Settings", "field": ("in", FFL_SETTINGS_FIELDS)})
	_report_dealer_store_orders()

	for doctype in sorted(doctypes) + ["FFL Settings"]:
		frappe.clear_cache(doctype=doctype)


# Rows elsewhere that name the doctype, as (doctype, filters). The desk ones are child rows of a
# Workspace / Workspace Sidebar / Sidebar: deleting the row takes the link off that page.
LEFTOVER_ROWS = (
	("Custom DocPerm", {"parent": SETTINGS_DOCTYPE}),
	("DocShare", {"share_doctype": SETTINGS_DOCTYPE}),
	("Workspace Link", {"link_type": "DocType", "link_to": SETTINGS_DOCTYPE}),
	("Workspace Shortcut", {"type": "DocType", "link_to": SETTINGS_DOCTYPE}),
	("Workspace Quick List", {"document_type": SETTINGS_DOCTYPE}),
	("Workspace Sidebar Item", {"link_type": "DocType", "link_to": SETTINGS_DOCTYPE}),
	("Sidebar Item", {"link_type": "DocType", "link_to": SETTINGS_DOCTYPE}),
)


def _remove_settings_doctype_leftovers() -> None:
	frappe.db.delete("Singles", {"doctype": SETTINGS_DOCTYPE})
	frappe.db.delete("__Auth", {"doctype": SETTINGS_DOCTYPE})
	for doctype in ("Property Setter", "Custom Field"):
		field = "doc_type" if doctype == "Property Setter" else "dt"
		for name in frappe.get_all(doctype, filters={field: SETTINGS_DOCTYPE}, pluck="name"):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
	for doctype, filters in LEFTOVER_ROWS:
		if frappe.db.table_exists(doctype):   # the sidebar doctypes vary by Frappe version
			frappe.db.delete(doctype, filters)


DEALER_ORDERS_TITLE = "1.11.0: Woo Online Orders from the removed dealer store"


def _report_dealer_store_orders() -> None:
	"""One Error Log naming every ``site='dealer'`` Woo Online Order (expected: none). Nothing is
	changed on them — no code path reads that site any more, so a human decides."""
	if not frappe.db.table_exists("Woo Online Order"):   # ffl_woo_sync not installed
		return
	names = frappe.get_all("Woo Online Order", filters={"site": "dealer"}, pluck="name",
		order_by="name asc")
	if names:
		frappe.log_error(title=DEALER_ORDERS_TITLE, message=(
			f"{len(names)} Woo Online Order(s) still say site='dealer'. 1.11.0 removed the dealer "
			"store, so nothing reads or syncs them; they were left unchanged:\n" + "\n".join(names)))
