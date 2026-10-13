"""``ffl_core.api.photos`` — the photo manager's server side.

Real Files on disk (tiny PNGs), real documents, real hooks: the contract under
test is "exactly one primary, ``image`` mirrors it, publish order = what the
operator arranged", and it has to hold on Serial No as well as Item now that
``sync_item_image_gallery`` runs on both.
"""
from __future__ import annotations

import base64
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api import photos
from ffl_core.images import ordered_gallery_paths

ITEM = "_TEST-PHOTO-ITEM"
SERIAL = "_TEST-PHOTO-SN-1"
# 1x1 transparent PNG.
_PNG = base64.b64decode(
	"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def _png(name: str, private: int = 1) -> str:
	"""A real File row with bytes on disk; returns its file_url.

	Unique bytes per call (random trailer after IEND, which decoders ignore):
	Frappe de-duplicates Files by content hash, and a privacy flip rewrites the
	URL of every row sharing the content — identical fixtures would make one
	test's publicize silently rename another test's private file."""
	import os
	f = frappe.get_doc({
		"doctype": "File", "file_name": f"{name}.png", "content": _PNG + os.urandom(12),
		"is_private": private,
	}).insert(ignore_permissions=True)
	return f.file_url


class PhotoFixtures(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("Item", ITEM):
			frappe.get_doc({
				"doctype": "Item", "item_code": ITEM, "item_name": ITEM,
				"item_group": "Firearm", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1,
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Serial No", SERIAL):
			frappe.get_doc({
				"doctype": "Serial No", "serial_no": SERIAL, "item_code": ITEM,
			}).insert(ignore_permissions=True)

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		# Keep every File's bytes on disk for the whole module: the DB rollback at
		# class end resurrects deleted File rows, and the runner then flips their
		# privacy back — which renames a file that must still exist.
		from frappe.core.doctype.file.file import File
		self.addCleanup(patch.stopall)
		patch.object(File, "delete_file_data_content", lambda self: None).start()
		for dt, name in (("Item", ITEM), ("Serial No", SERIAL)):
			doc = frappe.get_doc(dt, name)
			doc.set("image_gallery", [])
			doc.image = None
			doc.save(ignore_permissions=True)

	def _add(self, dt, name, n, prefix="p"):
		urls = [_png(f"{prefix}{i}-{frappe.generate_hash(length=6)}") for i in range(n)]
		return urls, photos.add_gallery_images(dt, name, urls)


class AddPhotos(PhotoFixtures):
	def test_first_upload_becomes_primary_and_files_go_public(self):
		urls, out = self._add("Item", ITEM, 3)
		self.assertEqual(len(out), 3)
		self.assertEqual([r["is_primary"] for r in out], [1, 0, 0])
		self.assertTrue(all(r["image"].startswith("/files/") for r in out),
			"uploads arrive private; the gallery must publish them")
		doc = frappe.get_doc("Item", ITEM)
		self.assertEqual(doc.image, out[0]["image"])
		self.assertEqual([r.sort_order for r in doc.image_gallery], [1, 2, 3])

	def test_second_upload_does_not_displace_the_featured_photo(self):
		_, first = self._add("Item", ITEM, 1)
		_, out = self._add("Item", ITEM, 2, prefix="q")
		self.assertEqual(out[0]["image"], first[0]["image"])
		self.assertEqual(sum(r["is_primary"] for r in out), 1)

	def test_duplicate_url_is_skipped_and_json_string_accepted(self):
		_, first = self._add("Item", ITEM, 1)
		# the uploader hands back the CURRENT (public) url; sending it again is a no-op
		out = photos.add_gallery_images("Item", ITEM, frappe.as_json([first[0]["image"]]))
		self.assertEqual(len(out), 1)

	def test_non_image_and_unknown_urls_are_refused(self):
		with self.assertRaises(frappe.ValidationError):
			photos.add_gallery_images("Item", ITEM, ["/files/notes.pdf"])
		with self.assertRaises(frappe.ValidationError):
			photos.add_gallery_images("Item", ITEM, ["/files/does-not-exist.png"])
		with self.assertRaises(frappe.ValidationError):
			photos.add_gallery_images("Item", ITEM, [])

	def test_only_item_and_serial_no_carry_a_gallery(self):
		with self.assertRaises(frappe.ValidationError):
			photos.add_gallery_images("Customer", "x", ["/files/a.png"])

	def test_serial_no_gets_the_same_primary_and_mirror_rules(self):
		_, out = self._add("Serial No", SERIAL, 2, prefix="s")
		self.assertEqual([r["is_primary"] for r in out], [1, 0])
		self.assertEqual(frappe.db.get_value("Serial No", SERIAL, "image"), out[0]["image"])


class ArrangePhotos(PhotoFixtures):
	def test_reorder_persists_and_drives_the_publish_order(self):
		_, out = self._add("Item", ITEM, 3)
		a, b, c = [r["name"] for r in out]
		out2 = photos.reorder_gallery("Item", ITEM, [a, c, b])
		self.assertEqual([r["name"] for r in out2], [a, c, b])
		doc = frappe.get_doc("Item", ITEM)
		self.assertEqual([r.sort_order for r in doc.image_gallery], [1, 2, 3])
		self.assertEqual(ordered_gallery_paths(doc.image_gallery),
			[r["image"] for r in out2], "the strip order IS the channel order")

	def test_reorder_cannot_demote_the_primary_below_first(self):
		_, out = self._add("Item", ITEM, 2)
		a, b = [r["name"] for r in out]
		out2 = photos.reorder_gallery("Item", ITEM, [b, a])
		self.assertEqual(out2[0]["name"], a, "primary always publishes first")

	def test_reorder_must_name_every_row_exactly_once(self):
		_, out = self._add("Item", ITEM, 2)
		with self.assertRaises(frappe.ValidationError):
			photos.reorder_gallery("Item", ITEM, [out[0]["name"]])

	def test_set_primary_is_exclusive_and_mirrors_image(self):
		_, out = self._add("Item", ITEM, 3)
		out2 = photos.set_primary_image("Item", ITEM, out[2]["name"])
		self.assertEqual(out2[0]["name"], out[2]["name"])
		self.assertEqual(sum(r["is_primary"] for r in out2), 1)
		self.assertEqual(frappe.db.get_value("Item", ITEM, "image"), out[2]["image"])

	def test_caption_round_trips(self):
		_, out = self._add("Item", ITEM, 1)
		out2 = photos.set_image_caption("Item", ITEM, out[0]["name"], "  left side  ")
		self.assertEqual(out2[0]["caption"], "left side")


class RemovePhotos(PhotoFixtures):
	def test_remove_deletes_the_file_only_when_unreferenced(self):
		urls, out = self._add("Item", ITEM, 2)
		shared = out[1]["image"]
		# the same photo also on a serial — must survive the Item-side removal
		photos.add_gallery_images("Serial No", SERIAL, [shared])
		photos.remove_gallery_image("Item", ITEM, out[1]["name"])
		self.assertTrue(frappe.db.exists("File", {"file_url": shared}), "still on the serial")
		sn_rows = photos.gallery(frappe.get_doc("Serial No", SERIAL))
		photos.remove_gallery_image("Serial No", SERIAL, sn_rows[0]["name"])
		self.assertFalse(frappe.db.exists("File", {"file_url": shared}), "last reference gone")

	def test_removing_the_primary_promotes_the_next_and_last_clears_image(self):
		_, out = self._add("Item", ITEM, 2)
		out2 = photos.remove_gallery_image("Item", ITEM, out[0]["name"])
		self.assertEqual(len(out2), 1)
		self.assertEqual(out2[0]["is_primary"], 1)
		self.assertEqual(frappe.db.get_value("Item", ITEM, "image"), out2[0]["image"])
		out3 = photos.remove_gallery_image("Item", ITEM, out2[0]["name"])
		self.assertEqual(out3, [])
		self.assertFalse(frappe.db.get_value("Item", ITEM, "image"))


class SerialsForItem(PhotoFixtures):
	def test_counts_and_thumbs_per_serial(self):
		self._add("Serial No", SERIAL, 2, prefix="t")
		rows = photos.serials_for_item(ITEM, in_stock_only=0)
		mine = next(r for r in rows if r["serial_no"] == SERIAL)
		self.assertEqual(mine["photo_count"], 2)
		self.assertEqual(len(mine["thumbs"]), 2)
		self.assertEqual(photos.serials_for_item(ITEM, in_stock_only=0, search="NOPE-"), [])


class Permissions(PhotoFixtures):
	def test_guest_cannot_touch_photos(self):
		urls = [_png("g")]
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				photos.add_gallery_images("Item", ITEM, urls)
			with self.assertRaises(frappe.PermissionError):
				photos.serials_for_item(ITEM)
		finally:
			frappe.set_user("Administrator")
