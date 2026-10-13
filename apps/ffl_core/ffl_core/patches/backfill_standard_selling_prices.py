import frappe
from frappe.utils import flt


def execute():
	"""One-time sweep of the sync_standard_selling_price rules over existing
	items: create the missing selling Item Price from standard_rate (items
	priced AFTER creation never got one — ERPNext only creates it at insert),
	and re-sync uniform-priced firearms whose Item Price drifted from
	standard_rate (the price the POS serial picker actually charges).

	Writes to the Selling Settings default price list (same as ERPNext's own
	Item.add_price); manager-curated prices on non-uniform items are never
	touched."""
	price_list = (
		frappe.db.get_single_value("Selling Settings", "selling_price_list")
		or frappe.db.get_value("Price List", "Standard Selling")
	)
	if not price_list:
		return

	items = frappe.get_all(
		"Item",
		filters={"disabled": 0, "is_sales_item": 1, "standard_rate": (">", 0)},
		fields=["name", "standard_rate", "uniform_sell_price", "has_serial_no", "stock_uom"],
	)
	for it in items:
		# Per-gun priced (serialized, non-uniform) items: no Item Price — it
		# would pre-rate POS lines and bypass the serial-sets-the-price flow.
		if it.has_serial_no and not it.uniform_sell_price:
			continue
		existing = frappe.db.get_value(
			"Item Price",
			{"item_code": it.name, "price_list": price_list, "selling": 1},
			["name", "price_list_rate"],
			as_dict=True,
		)
		if existing:
			if it.uniform_sell_price and flt(existing.price_list_rate) != flt(it.standard_rate):
				frappe.db.set_value("Item Price", existing.name,
					"price_list_rate", flt(it.standard_rate))
			continue
		frappe.get_doc({
			"doctype": "Item Price",
			"item_code": it.name,
			"price_list": price_list,
			"price_list_rate": flt(it.standard_rate),
			"selling": 1,
			"uom": it.stock_uom,
		}).insert(ignore_permissions=True)
