"""Product-photo fields default to PUBLIC uploads.

WooCommerce sideloads listing images with an anonymous GET, so a photo that
lands in ``/private/files`` 403s and the product push fails (400
``woocommerce_product_image_upload_error``).  The desk uploader picks Private
unless the field carries ``make_attachment_public`` or the DocType carries
``make_attachments_public``:

- ``Item``: ERPNext ships the DocType with ``make_attachments_public=1`` —
  nothing to add.
- ``Serial No``: DocType default is private ON PURPOSE (its sidebar holds
  4473s / ID scans), so only the ``image`` field flips — via a Property Setter
  (it is a Custom Field, which has no ``make_attachment_public`` column).
- ``Item Image Gallery.image``: our own DocType, flag set on the DocField —
  needed for Serial No galleries, whose parent form default is private.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

SETTER = "Serial No-image-make_attachment_public"


def _effective_public_default(doctype: str, fieldname: str) -> bool:
	"""Mirror of frappe/public/js/frappe/form/controls/attach.js:84-86 —
	``df.make_attachment_public ? 1 : frm.meta.make_attachments_public`` (field flag
	wins, else the DocType default).  Re-check those lines on a Frappe upgrade."""
	meta = frappe.get_meta(doctype)
	field = meta.get_field(fieldname)
	return bool(field.make_attachment_public or meta.make_attachments_public)


class TestListingImagePublicDefault(FrappeTestCase):
	def test_property_setter_fixture_present(self):
		path = frappe.get_app_path("ffl_core", "fixtures", "property_setter.json")
		with open(path) as fh:
			rows = {r["name"]: r for r in json.load(fh)}
		self.assertIn(SETTER, rows)
		row = rows[SETTER]
		self.assertEqual(row["doc_type"], "Serial No")
		self.assertEqual(row["doctype_or_field"], "DocField")
		self.assertEqual(row["field_name"], "image")
		self.assertEqual(row["property"], "make_attachment_public")
		self.assertEqual(row["property_type"], "Check")
		self.assertEqual(row["value"], "1")
		self.assertEqual(row["module"], "FFL Core")

	def test_serial_no_image_field_flag_applied_to_meta(self):
		field = frappe.get_meta("Serial No").get_field("image")
		self.assertEqual(field.fieldtype, "Attach Image")
		self.assertEqual(int(field.make_attachment_public or 0), 1)

	def test_gallery_row_image_field_is_public_by_default(self):
		field = frappe.get_meta("Item Image Gallery").get_field("image")
		self.assertEqual(field.fieldtype, "Attach Image")
		self.assertEqual(int(field.make_attachment_public or 0), 1)

	def test_every_listing_photo_field_defaults_public(self):
		self.assertTrue(_effective_public_default("Serial No", "image"))
		self.assertTrue(_effective_public_default("Item", "image"))
		self.assertTrue(_effective_public_default("Item Image Gallery", "image"))

	def test_serial_no_doctype_level_attachment_default_stays_private(self):
		"""Sidebar attachments on Serial No (4473s, ID scans) must keep Frappe's private default."""
		self.assertFalse(frappe.get_meta("Serial No").make_attachments_public)
