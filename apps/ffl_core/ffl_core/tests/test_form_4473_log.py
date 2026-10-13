"""Form 4473 Log — the durable record of every completed 4473, and the backfill
that puts the POS Invoices' existing 4473s on it."""
from __future__ import annotations

import unittest.mock

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.ffl_core.doctype.form_4473_log.form_4473_log import record_4473
from ffl_core.tests.test_pending_4473_orders import Pending4473Fixtures

PDF = "https://cloud.fastbound.com/1/api/Form4473s/Download/{}"
G1 = "11111111-2222-3333-4444-555555555501"
G2 = "11111111-2222-3333-4444-555555555502"


class Record4473(FrappeTestCase):
	def setUp(self):
		frappe.db.delete("Form 4473 Log", {"form4473_id": ("like", "L4473-%")})

	def test_same_form_id_updates_one_row_and_never_erases(self):
		name = record_4473(form4473_id="L4473-1", pos_invoices=["INV-A"], serials=["S1"],
			nics_status="Delayed", pdf_url="u1")
		again = record_4473(form4473_id="L4473-1", pos_invoices=[], serials=["S2"],
			nics_status="Proceed")
		self.assertEqual(name, again)
		row = frappe.get_doc("Form 4473 Log", name)
		self.assertEqual((row.pos_invoice, row.serials, row.nics_status, row.pdf_url),
			("INV-A", "S1, S2", "Proceed", "u1"))
		self.assertEqual(row.pdf_status, "Pending")

	def test_rows_without_a_form_id_are_separate_and_have_no_pdf(self):
		a = record_4473(pos_invoices=["INV-P"], source="Paper / override")
		b = record_4473(pos_invoices=["INV-P"], source="Paper / override")
		self.assertNotEqual(a, b)
		self.assertEqual(frappe.db.get_value("Form 4473 Log", a, "pdf_status"), "No PDF")
		self.assertIsNone(frappe.db.get_value("Form 4473 Log", a, "form4473_id"))
		frappe.db.delete("Form 4473 Log", {"name": ("in", [a, b])})

	def test_a_long_invoice_list_and_a_deleted_customer_still_record(self):
		invoices = [f"ACC-PSINV-2026-{i:05d}" for i in range(12)]
		name = record_4473(form4473_id="L4473-LONG", pos_invoices=invoices,
			customer="_No Such Customer 4473")
		self.assertEqual(frappe.db.get_value("Form 4473 Log", name, "pos_invoice"),
			", ".join(invoices))

	def test_cannot_be_deleted(self):
		name = record_4473(form4473_id="L4473-DEL")
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("Form 4473 Log", name, ignore_permissions=True)
		self.assertTrue(frappe.db.exists("Form 4473 Log", name))

	def test_nobody_may_create_or_delete_from_the_desk(self):
		for perm in frappe.get_meta("Form 4473 Log").permissions:
			self.assertEqual((perm.create, perm.delete, perm.write), (0, 0, 0), perm.role)


class BackfillForm4473Log(Pending4473Fixtures, FrappeTestCase):
	def _order(self, serial, *, pdf="", nics="Proceed", docstatus=None, amended_from=None):
		name = self._draft_invoice("P4473-GUN", serial=serial, form_4473="Completed", nics=nics)
		values = {"fastbound_4473_pdf_url": pdf, "fastbound_disposition_id": f"D-{serial}"}
		if docstatus is not None:
			values["docstatus"] = docstatus
		if amended_from:
			values["amended_from"] = amended_from
		frappe.db.set_value("POS Invoice", name, values, update_modified=False)
		return name

	def _logs(self, invoice):
		return frappe.get_all("Form 4473 Log", filters={"pos_invoice": ("like", f"%{invoice}%")},
			fields=["name", "form4473_id", "source", "pos_invoice", "serials", "nics_status",
				"pdf_status", "fastbound_disposition_id"])

	def _run(self):
		from ffl_core.patches.backfill_form_4473_log import execute
		execute()

	def test_backfill_is_idempotent_and_keys_fastbound_4473s_by_form_id(self):
		fb = self._order("B4473-FB", pdf=PDF.format(G1), nics="Denied")
		paper = self._order("B4473-PA")
		frappe.get_doc({"doctype": "FFL Disposition", "name": f"_T-BF-{paper}",
			"disposition_type": "Retail Sale", "linked_pos_invoice": paper,
			"serial": "B4473-PA", "manual_override": 1, "docstatus": 1}).db_insert()
		repaired = self._order("B4473-RP")  # FastBound 4473 whose link never arrived
		self._run()
		self._run()
		(row,) = self._logs(fb)
		self.assertEqual((row.form4473_id, row.source, row.nics_status, row.pdf_status, row.serials),
			(G1, "FastBound", "Denied", "Pending", "B4473-FB"))
		self.assertEqual(row.fastbound_disposition_id, "D-B4473-FB")
		(row,) = self._logs(paper)
		self.assertEqual((row.source, row.pdf_status), ("Paper / override", "No PDF"))
		(row,) = self._logs(repaired)
		self.assertEqual((row.source, row.pdf_status), ("FastBound", "No PDF"))

	def test_one_bad_row_is_skipped_never_the_migrate(self):
		bad = self._order("B4473-BAD", pdf=PDF.format("11111111-2222-3333-4444-555555555503"))
		good = self._order("B4473-GOOD", pdf=PDF.format("11111111-2222-3333-4444-555555555504"))
		from ffl_core.patches import backfill_form_4473_log as patch_module
		real = patch_module._record
		def flaky(name, *a, **k):
			if name == bad:
				raise RuntimeError("odd legacy row")
			return real(name, *a, **k)
		with unittest.mock.patch.object(patch_module, "_record", side_effect=flaky):
			self._run()
		self.assertEqual(self._logs(bad), [])
		self.assertEqual(len(self._logs(good)), 1)

	def test_an_amended_order_is_one_4473(self):
		original = self._order("B4473-AM", pdf=PDF.format(G2), docstatus=2)
		amended = self._order("B4473-AM", pdf=PDF.format(G2), amended_from=original)
		plain_original = self._order("B4473-AP", docstatus=2)
		plain_amended = self._order("B4473-AP", amended_from=plain_original)
		self._run()
		(row,) = frappe.get_all("Form 4473 Log", filters={"form4473_id": G2},
			fields=["pos_invoice"])
		self.assertEqual(row.pos_invoice, f"{original}, {amended}")
		self.assertEqual(self._logs(plain_original), [])
		self.assertEqual(len(self._logs(plain_amended)), 1)
