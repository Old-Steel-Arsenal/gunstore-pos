"""Tests for the Item.image <-> image_gallery sync hook.

Model: the gallery is the full set of photos; exactly one row is primary and
Item.image (the standard top-right / featured widget) mirrors that primary.
Uploading via the top-right widget adds a photo but never steals the featured
slot — the featured image is changed only by ticking `is_primary` on a row.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.item_hooks import sync_item_image_gallery


def _new_item(item_code: str, **kw):
	frappe.delete_doc_if_exists("Item", item_code)
	return frappe.get_doc({
		"doctype": "Item",
		"item_code": item_code,
		"item_name": kw.pop("item_name", item_code),
		"item_group": kw.pop("item_group", "All Item Groups"),
		"stock_uom": "Nos",
		"is_stock_item": 1,
		**kw,
	})


def _primary(doc):
	return [r for r in (doc.image_gallery or []) if r.is_primary]


class ItemImageGallerySync(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import test_bootstrap
		test_bootstrap.run()
		frappe.set_user("Administrator")

	def test_top_right_image_added_to_gallery_as_primary_when_empty(self):
		# Upload a top-right image on an item with no gallery → it lands in the
		# gallery and (being the only photo) becomes primary.
		doc = _new_item("TEST-IMG-1", image="/files/a.jpg")
		doc.insert(ignore_permissions=True)
		self.assertEqual(len(doc.image_gallery), 1)
		self.assertEqual(doc.image_gallery[0].image, "/files/a.jpg")
		self.assertEqual(doc.image_gallery[0].is_primary, 1)
		self.assertEqual(doc.image, "/files/a.jpg")

	def test_gallery_image_mirrored_to_top_right_when_no_image(self):
		# Add a photo only to the gallery → the top-right image fills from it.
		doc = _new_item("TEST-IMG-2")
		doc.append("image_gallery", {"image": "/files/b.jpg"})
		doc.insert(ignore_permissions=True)
		self.assertEqual(doc.image, "/files/b.jpg")
		self.assertEqual(_primary(doc)[0].image, "/files/b.jpg")

	def test_top_right_upload_added_as_extra_keeps_featured(self):
		# Featured photo already set; uploading a new top-right image adds it as
		# an extra and leaves the featured (primary) photo unchanged.
		doc = _new_item("TEST-IMG-3", image="/files/a.jpg")
		doc.insert(ignore_permissions=True)
		doc.image = "/files/b.jpg"
		doc.save(ignore_permissions=True)
		urls = sorted(r.image for r in doc.image_gallery)
		self.assertEqual(urls, ["/files/a.jpg", "/files/b.jpg"])
		self.assertEqual(_primary(doc)[0].image, "/files/a.jpg")
		self.assertEqual(doc.image, "/files/a.jpg")

	def test_ticking_is_primary_changes_featured(self):
		# Ticking is_primary on a gallery row is the one way to change the
		# featured image → Item.image follows it and the old primary is cleared.
		doc = _new_item("TEST-IMG-4", image="/files/a.jpg")
		doc.append("image_gallery", {"image": "/files/b.jpg"})
		doc.insert(ignore_permissions=True)
		for r in doc.image_gallery:
			if r.image == "/files/b.jpg":
				r.is_primary = 1
		doc.save(ignore_permissions=True)
		self.assertEqual(doc.image, "/files/b.jpg")
		self.assertEqual(len(_primary(doc)), 1)
		self.assertEqual(_primary(doc)[0].image, "/files/b.jpg")

	def test_multiple_primaries_collapsed_to_one(self):
		# Several rows flagged primary at once (e.g. an RSR import) → exactly one
		# survives and Item.image mirrors it.
		doc = _new_item("TEST-IMG-5")
		doc.append("image_gallery", {"image": "/files/a.jpg", "is_primary": 1})
		doc.append("image_gallery", {"image": "/files/b.jpg", "is_primary": 1})
		doc.insert(ignore_permissions=True)
		self.assertEqual(len(_primary(doc)), 1)
		self.assertEqual(doc.image, _primary(doc)[0].image)

	def test_idempotent(self):
		doc = _new_item("TEST-IMG-6", image="/files/a.jpg")
		doc.insert(ignore_permissions=True)
		sync_item_image_gallery(doc)
		sync_item_image_gallery(doc)
		self.assertEqual(len(doc.image_gallery), 1)
		self.assertEqual(_primary(doc)[0].image, "/files/a.jpg")

	def test_no_image_no_gallery_is_noop(self):
		doc = _new_item("TEST-IMG-7")
		doc.insert(ignore_permissions=True)
		self.assertEqual(len(doc.image_gallery or []), 0)
		self.assertFalse(doc.image)
