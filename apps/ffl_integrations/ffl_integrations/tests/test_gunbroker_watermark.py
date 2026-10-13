"""GunBroker photo watermark and the in-place photo swap.

The watermark is drawn with real Pillow on real JPEG bytes; the swap runs
against a fake GunBroker that renumbers a listing's pictures on every upload and
delete, which is what the sandbox showed (10-11) and what makes a picture id
read earlier useless.
"""

from __future__ import annotations

import io
from contextlib import ExitStack
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from PIL import Image

from ffl_integrations.gunbroker import listings, mapping


def _jpeg(size=(400, 300), colour=(255, 255, 255)) -> bytes:
	out = io.BytesIO()
	Image.new("RGB", size, colour).save(out, "JPEG")
	return out.getvalue()


def _logo() -> bytes:
	"""Black block on white: the shape the logo file is expected to have."""
	img = Image.new("RGB", (200, 50), (255, 255, 255))
	img.paste((0, 0, 0), (20, 10, 180, 40))
	out = io.BytesIO()
	img.save(out, "PNG")
	return out.getvalue()


class DrawingTheWatermark(FrappeTestCase):
	def test_the_mark_lands_in_the_lower_right_and_nowhere_else(self):
		out = Image.open(io.BytesIO(mapping.apply_watermark(_jpeg(), _logo(), "example.com")))
		self.assertEqual(out.size, (400, 300))
		self.assertEqual(out.format, "JPEG")
		gray = out.convert("L")
		corner = min(gray.getpixel((x, y)) for x in range(280, 395) for y in range(240, 295))
		self.assertLess(corner, 150, "logo ink should darken the lower right")
		self.assertGreater(gray.getpixel((20, 20)), 245, "the rest of the photo is untouched")

	def test_text_alone_is_enough(self):
		out = Image.open(io.BytesIO(mapping.apply_watermark(_jpeg(), b"", "example.com")))
		gray = out.convert("L")
		corner = min(gray.getpixel((x, y)) for x in range(280, 395) for y in range(240, 295))
		self.assertLess(corner, 200)

	def test_a_transparent_logo_ground_is_not_ink(self):
		img = Image.new("RGBA", (200, 50), (0, 0, 0, 0))
		img.paste((0, 0, 0, 255), (20, 10, 180, 40))
		buf = io.BytesIO()
		img.save(buf, "PNG")
		gray = Image.open(io.BytesIO(mapping.apply_watermark(_jpeg(), buf.getvalue(), ""))).convert("L")
		# The logo spans x 280-388, y 261-288 on this 400x300 photo; its transparent
		# left margin (logo x 0-19 -> photo x 280-290) must stay light.
		margin = [gray.getpixel((x, y)) for x in range(281, 289) for y in range(263, 286)]
		self.assertGreater(min(margin), 200, "the transparent margin stays light, not inked")

	def test_no_logo_and_no_text_means_no_watermark(self):
		self.assertIsNone(mapping.watermark_spec(frappe._dict()))


class CollectingThePhotos(FrappeTestCase):
	def _parts(self, mark, **patches):
		with ExitStack() as stack:
			stack.enter_context(patch.object(mapping, "_image_part",
				return_value=(("main.png", _jpeg(), "image/png"), "")))
			stack.enter_context(patch.object(mapping, "watermark_spec", return_value=mark))
			for name, value in patches.items():
				stack.enter_context(patch.object(mapping, name, value))
			return mapping._picture_parts(["/files/main.png"], [], frappe._dict())

	def test_a_watermarked_photo_goes_up_as_a_jpeg(self):
		built = self._parts({"logo": _logo(), "text": "example.com"})
		filename, content, content_type = built["parts"][0]
		self.assertEqual((filename, content_type), ("main.jpg", "image/jpeg"))
		self.assertEqual(Image.open(io.BytesIO(content)).format, "JPEG")

	def test_a_photo_that_cannot_be_marked_still_goes_up(self):
		def _boom(*a, **k):
			raise OSError("truncated")
		built = self._parts({"logo": b"", "text": "x"}, apply_watermark=_boom)
		self.assertEqual(built["parts"][0][0], "main.png", "the original photo, unmarked")
		self.assertIn("watermark_failed", built["warnings"])

	def test_without_a_watermark_the_photo_is_sent_as_it_is(self):
		built = self._parts(None)
		self.assertEqual(built["parts"][0][0], "main.png")


class _RenumberingGunBroker:
	"""Pictures keyed by file name; every id changes after any upload or delete."""

	def __init__(self, names):
		self.names = list(names)
		self.epoch = 0
		self.calls: list[str] = []
		self.uploaded = 0

	def _ids(self):
		return {1000 * (self.epoch + 1) + i: n for i, n in enumerate(self.names)}

	def list_pictures(self, item_id):
		return {"results": [{"pictureID": pid, "fileName": n} for pid, n in self._ids().items()]}

	def add_pictures(self, item_id, parts):
		self.calls.append("add")
		for _part in parts:
			self.uploaded += 1
			self.names.append(f"new{self.uploaded}")
		self.epoch += 1

	def delete_picture(self, picture_id):
		self.calls.append("delete")
		name = self._ids().get(picture_id)
		if name is None:
			return False
		self.names.remove(name)
		self.epoch += 1
		return True


class ReplacingALiveListingsPhotos(FrappeTestCase):
	def _run(self, row, parts=3, old=("pixA", "pixB")):
		gb = _RenumberingGunBroker(old)
		built = {"parts": [(f"p{i}.jpg", b"x", "image/jpeg") for i in range(parts)], "warnings": []}
		with patch("frappe.db.get_value", return_value=frappe._dict(row)), \
				patch.object(mapping, "picture_parts", return_value=built), \
				patch.object(mapping, "item_picture_parts", return_value=built):
			result = listings.replace_pictures("Serial No", "SN-1", client=gb)
		return result, gb

	def test_new_photos_go_up_first_and_every_old_one_comes_off(self):
		result, gb = self._run({"gb_item_id": "123"}, parts=10)
		self.assertTrue(result["ok"])
		self.assertEqual(gb.calls, ["add", "delete", "delete", "add"],
			"first batch, then the old photos off, then the rest: never more than old + 8")
		self.assertEqual(gb.names, [f"new{i}" for i in range(1, 11)])
		self.assertEqual(result["removed"], 2)

	def test_an_auction_with_bids_is_left_alone(self):
		result, gb = self._run({"gb_item_id": "123", "gb_bid_count": 2})
		self.assertEqual(result["skipped"], listings.AUCTION_HAS_BIDS)
		self.assertEqual(gb.calls, [])

	def test_a_gun_not_on_gunbroker_is_refused(self):
		result, gb = self._run({"gb_item_id": None})
		self.assertEqual(result["skipped"], listings.UNLISTED)
		self.assertEqual(gb.calls, [])
