"""view_4473_pdf — shows a Form 4473 Log row's signed PDF, behind "View PDF" /
"View Signed 4473".

The private copy when the row has one; otherwise live from this site's
FastBound account (the stored download link needs the API key, so a browser
click on it is a 401), queuing a copy.
"""
from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.ffl_core.doctype.form_4473_log.form_4473_log import record_4473
from ffl_integrations.fastbound.client import FastBoundConfigError, FastBoundError

GUID = "9943f770-a955-421e-b0ef-b4ce013cd798"
_CLIENT = "ffl_integrations.fastbound.pos_4473.FastBoundClient.from_settings_fast"


def _pdf() -> bytes:
	"""A real one-page PDF: File validates a .pdf's content on insert."""
	from pypdf import PdfWriter
	writer, buf = PdfWriter(), io.BytesIO()
	writer.add_blank_page(width=72, height=72)
	writer.write(buf)
	return buf.getvalue()


PDF = _pdf()  # once: a writer stamps a fresh document id per call


class View4473Pdf(FrappeTestCase):
	def setUp(self):
		frappe.local.response = frappe._dict()
		frappe.db.delete("Form 4473 Log", {"form4473_id": GUID})
		self.log = record_4473(form4473_id=GUID, pos_invoices=["ACC-PSINV-T-1"],
			nics_status="Proceed", pdf_url=f"https://cloud.fastbound.com/1/api/Form4473s/Download/{GUID}")

	def _view(self, log=None):
		from ffl_integrations.fastbound.pos_4473 import view_4473_pdf
		return view_4473_pdf(log or self.log)

	def _client(self, pdf=b"%PDF-1.7 live"):
		client = MagicMock()
		client.download_4473_pdf.return_value = pdf
		return client

	def test_no_copy_yet_fetches_live_by_form_id_and_queues_a_copy(self):
		client = self._client()
		with patch(_CLIENT, return_value=client), patch("frappe.enqueue") as enqueue:
			self._view()
		client.download_4473_pdf.assert_called_once_with(GUID)
		self.assertEqual(frappe.local.response.type, "pdf")
		self.assertEqual(frappe.local.response.filecontent, b"%PDF-1.7 live")
		self.assertEqual(frappe.local.response.filename, "4473-ACC-PSINV-T-1.pdf")
		kwargs = enqueue.call_args.kwargs
		self.assertEqual((kwargs["log_name"], kwargs["force"]), (self.log, True))
		# A GET is rolled back, never committed: an after-commit job would be dropped.
		self.assertFalse(kwargs.get("enqueue_after_commit"))
		self.assertTrue(frappe.db.exists("Access Log", {
			"export_from": "Form 4473 Log", "reference_document": self.log, "file_type": "PDF"}))

	def test_stored_copy_is_served_without_calling_fastbound(self):
		f = frappe.get_doc({"doctype": "File", "file_name": "4473-copy.pdf", "is_private": 1,
			"attached_to_doctype": "Form 4473 Log", "attached_to_name": self.log,
			"content": PDF})
		f.insert(ignore_permissions=True)
		frappe.db.set_value("Form 4473 Log", self.log,
			{"pdf_file": f.file_url, "pdf_status": "Stored"})
		with patch(_CLIENT) as factory:
			self._view()
		factory.assert_not_called()
		self.assertEqual(frappe.local.response.filecontent, PDF)

	def test_paper_row_explains_there_is_no_pdf(self):
		paper = record_4473(pos_invoices=["ACC-PSINV-T-2"], source="Paper / override")
		with patch(_CLIENT) as factory, self.assertRaises(frappe.ValidationError) as ctx:
			self._view(paper)
		factory.assert_not_called()
		self.assertIn("no signed 4473 PDF", str(ctx.exception))

	def test_role_without_4473_access_is_refused(self):
		with patch("frappe.get_roles", return_value=["Stock User"]), \
				patch(_CLIENT) as factory, self.assertRaises(frappe.PermissionError):
			self._view()
		factory.assert_not_called()

	def test_fastbound_not_found_is_readable(self):
		client = MagicMock()
		client.download_4473_pdf.side_effect = FastBoundError("GET → 404", status_code=404)
		with patch(_CLIENT, return_value=client), self.assertRaises(frappe.ValidationError) as ctx:
			self._view()
		self.assertIn("could not find", str(ctx.exception))

	def test_a_row_the_sweep_gave_up_on_is_still_tried_live(self):
		frappe.db.set_value("Form 4473 Log", self.log, "pdf_status", "Not Found")
		client = self._client()
		with patch(_CLIENT, return_value=client), patch("frappe.enqueue"):
			self._view()
		client.download_4473_pdf.assert_called_once_with(GUID)

	def test_fastbound_not_configured_is_readable(self):
		with patch(_CLIENT, side_effect=FastBoundConfigError("disabled")), \
				self.assertRaises(frappe.ValidationError) as ctx:
			self._view()
		self.assertIn("FastBound", str(ctx.exception))
