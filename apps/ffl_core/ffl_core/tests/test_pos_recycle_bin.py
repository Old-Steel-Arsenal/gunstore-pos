"""POS recycle bin: list + restore deleted POS Invoice drafts (api/pos_recycle_bin)."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api import pos_recycle_bin

_CUSTOMER = "_Test Recycle Bin Buyer"
_ITEM = "RECYCLE-BIN-SVC"


def _draft() -> str:
	inv = frappe.new_doc("POS Invoice")
	inv.pos_profile = "FFL Test"
	inv.customer = _CUSTOMER
	inv.append("items", {"item_code": _ITEM, "qty": 1, "rate": 25, "price_list_rate": 25})
	inv.set_missing_values()
	inv.set("payments", [])
	inv.append("payments", {"mode_of_payment": "Cash", "amount": 25})
	inv.flags.ignore_permissions = True
	inv.save()
	return inv.name


def _delete(name: str) -> str:
	frappe.delete_doc("POS Invoice", name, ignore_permissions=True)
	# Newest row: the class-level rollback keeps earlier tests' log rows, and
	# the rewound series re-issues the same number to later drafts.
	return frappe.db.get_value("Deleted Document",
		{"deleted_doctype": "POS Invoice", "deleted_name": name, "restored": 0},
		"name", order_by="creation desc")


class PosRecycleBin(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		if not frappe.db.exists("Customer", _CUSTOMER):
			frappe.get_doc({"doctype": "Customer", "customer_name": _CUSTOMER,
				"customer_group": "Individual", "territory": "All Territories"}).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", _ITEM):
			frappe.get_doc({"doctype": "Item", "item_code": _ITEM, "item_name": _ITEM,
				"item_group": "Ammunition", "stock_uom": "Nos", "is_stock_item": 0}).insert(ignore_permissions=True)
		from ffl_core.tests.pos_opening import todays_pos_opening
		todays_pos_opening(dev_site.COMPANY)

	def test_deleted_draft_is_listed_then_restored_under_its_own_number(self):
		name = _draft()
		owner = frappe.db.get_value("POS Invoice", name, "owner")
		log = _delete(name)
		listed = [r for r in pos_recycle_bin.list_deleted_invoices() if r["name"] == name]
		self.assertEqual(len(listed), 1)
		self.assertEqual(listed[0]["deleted_document"], log)
		self.assertEqual(listed[0]["customer_name"], _CUSTOMER)
		self.assertEqual(listed[0]["grand_total"], 25)
		# search matches number or customer, case-insensitively
		self.assertTrue(pos_recycle_bin.list_deleted_invoices(search_term=name.lower()))
		self.assertTrue(pos_recycle_bin.list_deleted_invoices(search_term="recycle bin buyer"))
		self.assertFalse([r for r in pos_recycle_bin.list_deleted_invoices(search_term="zzz-no-such")
			if r["name"] == name])

		out = pos_recycle_bin.restore_deleted_invoice(log)
		self.assertEqual(out, {"name": name, "original_name": name, "renamed": False})
		self.assertEqual(frappe.db.get_value("POS Invoice", name, ["docstatus", "owner", "customer"]),
			(0, owner, _CUSTOMER))
		self.assertEqual(frappe.db.get_value("Deleted Document", log, ["restored", "new_name"]), (1, name))
		# gone from the bin; a second restore is refused
		self.assertFalse([r for r in pos_recycle_bin.list_deleted_invoices() if r["name"] == name])
		self.assertRaises(frappe.DocumentAlreadyRestored, pos_recycle_bin.restore_deleted_invoice, log)
		# Deleting the last doc of a series rewinds its counter; the restore
		# must move it back up or the next new draft collides with this one.
		self.assertNotEqual(_draft(), name)

	def test_taken_number_falls_back_to_a_new_one(self):
		name = _draft()
		log = _delete(name)
		# Something else grabbed the number in the meantime (naming series
		# never re-issues, so simulate it by renaming a fresh draft onto it).
		other = _draft()
		if other != name:   # series rewound on delete → usually re-issued as-is
			frappe.rename_doc("POS Invoice", other, name, force=True, show_alert=False)
		out = pos_recycle_bin.restore_deleted_invoice(log)
		self.assertTrue(out["renamed"])
		self.assertNotEqual(out["name"], name)
		self.assertTrue(frappe.db.exists("POS Invoice", out["name"]))

	def test_non_draft_deletions_are_hidden_and_refused(self):
		name = _draft()
		doc = frappe.get_doc("POS Invoice", name)
		doc.flags.ignore_permissions = True
		doc.submit()
		doc.reload()
		doc.cancel()
		log = _delete(name)
		self.assertFalse([r for r in pos_recycle_bin.list_deleted_invoices() if r["name"] == name])
		self.assertRaises(frappe.ValidationError, pos_recycle_bin.restore_deleted_invoice, log)

	def test_search_matches_customer_name_beyond_the_scan_of_newer_rows(self):
		name = _draft()
		log = _delete(name)
		# Newer, non-restorable noise on top: the search must still find the draft.
		for _ in range(3):
			d = _draft()
			doc = frappe.get_doc("POS Invoice", d)
			doc.flags.ignore_permissions = True
			doc.submit()
			doc.reload()
			doc.cancel()
			_delete(d)
		hits = pos_recycle_bin.list_deleted_invoices(search_term="RECYCLE BIN buyer")
		self.assertIn(log, [h["deleted_document"] for h in hits])
		self.assertTrue(all(h["customer_name"] == _CUSTOMER for h in hits))

	def test_restore_refuses_rows_older_than_the_retention_window(self):
		name = _draft()
		log = _delete(name)
		frappe.db.set_value("Deleted Document", log, "creation",
			frappe.utils.add_days(frappe.utils.now_datetime(), -(pos_recycle_bin.RETENTION_DAYS + 1)),
			update_modified=False)
		self.assertNotIn(log, [r["deleted_document"] for r in pos_recycle_bin.list_deleted_invoices()])
		self.assertRaises(frappe.ValidationError, pos_recycle_bin.restore_deleted_invoice, log)

	def test_requires_pos_invoice_delete_permission(self):
		name = _draft()
		log = _delete(name)
		frappe.set_user("Guest")
		try:
			self.assertRaises(frappe.PermissionError, pos_recycle_bin.list_deleted_invoices)
			self.assertRaises(frappe.PermissionError, pos_recycle_bin.restore_deleted_invoice, log)
		finally:
			frappe.set_user("Administrator")
