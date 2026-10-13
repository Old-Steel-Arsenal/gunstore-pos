"""First-sale smoke test: open a POS shift, sell one non-firearm item for cash, close the shift.

Needs no FastBound, distributor or WooCommerce account: it proves that the Company, POS Profile,
sales tax, stock and the POS-to-ledger merge work on a new site. Run it once, after
``scripts/store_setup.sh``:

    bench --site <site> execute ffl_core.setup.first_sale_check.run

It does what the cashier does on the POS screen, with real documents. A passing run keeps them: the stock
item ``FIRST-SALE-CHECK`` (an Accessory, 5 received once), and per run one cash sale (a POS Invoice and its
merged Sales Invoice) and one closed shift. A failed step raises (non-zero exit) and names itself. A failure
after the shift is closed leaves the test documents in place (ERPNext's merge commits as it goes); remove
them with the cancel recipe in docs/self-hosting.md "Verify a first sale". An earlier failure normally leaves nothing.
Because it posts to the ledger it refuses to run on a site that already has other sales; pass
``force=1`` only on a test site. Without ``pos_profile`` it uses the store's only enabled POS Profile, and
refuses when there are several (a test sale must not land in another company's books).
It prints ``FIRST_SALE_CHECK PASS`` last.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime, nowdate

from ffl_core.utils import commit

ITEM = "FIRST-SALE-CHECK"
PRICE = 20
MARKER = "FIRST_SALE_CHECK PASS"


def log(*parts) -> None:
	print("FIRST_SALE_CHECK", *parts)


def _profile(pos_profile: str | None):
	if pos_profile:
		return frappe.get_doc("POS Profile", pos_profile)
	names = frappe.get_all("POS Profile", filters={"disabled": 0}, fields=["name", "company"], order_by="creation")
	if not names:
		frappe.throw(_("There is no POS Profile: run scripts/store_setup.sh first."))
	if len(names) > 1:
		frappe.throw(_("There is more than one enabled POS Profile ({0}). Say which: bench execute "
			"ffl_core.setup.first_sale_check.run --kwargs \"{{'pos_profile': '<name>'}}\".").format(
			", ".join(f"{n.name} ({n.company})" for n in names)))
	return frappe.get_doc("POS Profile", names[0].name)


def _refuse_on_a_live_ledger(company: str) -> None:
	"""Any submitted sale that is not this check's own means a real ledger."""
	others = 0
	for doctype in ("POS Invoice", "Sales Invoice"):
		others += frappe.db.sql(
			f"""SELECT COUNT(*) FROM `tab{doctype}` i WHERE i.docstatus = 1 AND i.company = %s
			AND NOT EXISTS (SELECT 1 FROM `tab{doctype} Item` t WHERE t.parent = i.name AND t.item_code = %s)""",
			(company, ITEM))[0][0]
	if others:
		frappe.throw(_("{0} already has {1} sales. This check posts a test sale to the ledger, so it runs only on a "
			"new site. On a live store follow the Desk steps in docs/self-hosting.md instead.").format(company, others))


def _item(profile) -> None:
	if not frappe.db.exists("Item", ITEM):
		frappe.get_doc({
			"doctype": "Item", "item_code": ITEM, "item_name": "First sale check (test item)",
			"item_group": "Accessory", "stock_uom": frappe.db.get_single_value("Stock Settings", "stock_uom") or "Nos",
			"is_stock_item": 1, "has_serial_no": 0, "standard_rate": PRICE,
		}).insert(ignore_permissions=True)
	if not frappe.db.get_value("Bin", {"item_code": ITEM, "warehouse": profile.warehouse}, "actual_qty"):
		receipt = frappe.get_doc({
			"doctype": "Stock Entry", "stock_entry_type": "Material Receipt", "company": profile.company,
			"items": [{"item_code": ITEM, "qty": 5, "t_warehouse": profile.warehouse, "basic_rate": 10}],
		})
		receipt.flags.ignore_permissions = True
		receipt.insert()
		receipt.submit()
		log("stocked 5 x", ITEM, "in", profile.warehouse)


def run(pos_profile: str | None = None, force: int = 0) -> dict:
	from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import make_closing_entry_from_opening

	frappe.set_user("Administrator")
	profile = _profile(pos_profile)
	company = profile.company
	if not cint(force):
		_refuse_on_a_live_ledger(company)
	if frappe.db.exists("POS Opening Entry", {"pos_profile": profile.name, "status": "Open", "docstatus": 1}):
		frappe.throw(_("{0} already has an open shift. Close it on the POS screen first.").format(profile.name))
	_item(profile)

	opening = frappe.get_doc({
		"doctype": "POS Opening Entry", "company": company, "pos_profile": profile.name, "user": "Administrator",
		"period_start_date": now_datetime(), "posting_date": nowdate(),
		"balance_details": [{"mode_of_payment": "Cash", "opening_amount": 0}],
	})
	opening.flags.ignore_permissions = True
	opening.insert()
	opening.submit()
	log("shift opened", opening.name)

	sale = frappe.new_doc("POS Invoice")
	sale.pos_profile = profile.name
	sale.customer = profile.customer
	sale.append("items", {"item_code": ITEM, "qty": 1, "rate": PRICE, "price_list_rate": PRICE})
	sale.set_missing_values()
	sale.items[0].rate = sale.items[0].price_list_rate = PRICE
	sale.calculate_taxes_and_totals()
	sale.set("payments", [])
	sale.append("payments", {"mode_of_payment": "Cash", "amount": sale.grand_total})
	sale.flags.ignore_permissions = True
	sale.save()
	sale.submit()
	tax = flt(sale.total_taxes_and_charges)
	log("cash sale", sale.name, f"net {PRICE:.2f} + tax {tax:.2f} = {flt(sale.grand_total):.2f}")
	if profile.taxes_and_charges and not tax:
		frappe.throw(_("The sale carries no sales tax although the POS Profile has a tax template."))

	closing = make_closing_entry_from_opening(opening)
	for row in closing.payment_reconciliation:
		row.closing_amount = row.expected_amount  # the cash counted equals the cash expected
	closing.flags.ignore_permissions = True
	closing.insert()
	closing.submit()
	log("shift closed", closing.name)

	merged = frappe.db.get_value("POS Invoice", sale.name, "consolidated_invoice")
	if not merged:
		frappe.throw(_("The shift closed but the merged Sales Invoice for {0} is missing. Check the Error Log and "
			"POS Closing Entry {1}.").format(sale.name, closing.name))
	si = frappe.get_doc("Sales Invoice", merged)
	gl = frappe.db.sql(
		"""SELECT IFNULL(SUM(debit), 0), IFNULL(SUM(credit), 0) FROM `tabGL Entry`
		WHERE voucher_type = 'Sales Invoice' AND voucher_no = %s AND is_cancelled = 0""", merged)[0]
	if not gl[0] or round(gl[0] - gl[1], 2):
		frappe.throw(_("{0} posted an unbalanced or empty ledger entry: debit {1}, credit {2}.").format(merged, gl[0], gl[1]))
	if flt(si.outstanding_amount):
		frappe.throw(_("{0} is not fully paid: outstanding {1}.").format(merged, si.outstanding_amount))
	log("ledger", merged, f"paid, debit {flt(gl[0]):.2f} = credit {flt(gl[1]):.2f}")
	commit()
	print(MARKER)
	return {"opening": opening.name, "pos_invoice": sale.name, "sales_invoice": merged, "closing": closing.name,
		"total": flt(sale.grand_total), "tax": tax}
