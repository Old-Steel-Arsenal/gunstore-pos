"""``ffl_core.utils.set_file_privacy`` — the one home for File visibility flips.

``receive_goods._ensure_private_file`` (Gov-ID scans → private) and
``ffl_woo_sync.woocommerce.mapping._publicize_file`` (product photos → public)
are thin wrappers over it.  Frappe's own move mechanics are exercised by
``ffl_woo_sync.tests.test_mapping.TestPublicizeFileOnDisk``; here the contract.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.api.receive_goods import _ensure_private_file
from ffl_core.utils import set_file_privacy


def _file_doc(url: str, is_private: int) -> MagicMock:
	"""File mock whose save() performs Frappe's private<->public URL rewrite."""
	file_doc = MagicMock()
	file_doc.is_private = is_private
	file_doc.file_url = url

	def _save(**kwargs):
		base = url.rsplit("/", 1)[-1]
		file_doc.file_url = ("/private/files/" if file_doc.is_private else "/files/") + base
	file_doc.save.side_effect = _save
	return file_doc


class TestSetFilePrivacy(FrappeTestCase):
	def test_blank_returns_none_without_db(self):
		with patch("frappe.db.get_value") as get_value:
			self.assertIsNone(set_file_privacy("", private=True))
			self.assertIsNone(set_file_privacy(None, private=False))
		get_value.assert_not_called()

	def test_missing_row_returns_none(self):
		with patch("frappe.db.get_value", return_value=None), patch("frappe.get_doc") as get_doc:
			self.assertIsNone(set_file_privacy("/files/x.jpg", private=True))
		get_doc.assert_not_called()

	def test_public_to_private(self):
		file_doc = _file_doc("/files/id.jpg", is_private=0)
		with patch("frappe.db.get_value", return_value="F1"), patch("frappe.get_doc", return_value=file_doc):
			self.assertEqual(set_file_privacy("/files/id.jpg", private=True), "/private/files/id.jpg")
		self.assertEqual(file_doc.is_private, 1)
		file_doc.save.assert_called_once_with(ignore_permissions=True)

	def test_private_to_public(self):
		file_doc = _file_doc("/private/files/gun.jpg", is_private=1)
		with patch("frappe.db.get_value", return_value="F1"), patch("frappe.get_doc", return_value=file_doc):
			self.assertEqual(set_file_privacy("/private/files/gun.jpg", private=False), "/files/gun.jpg")
		self.assertEqual(file_doc.is_private, 0)
		file_doc.save.assert_called_once_with(ignore_permissions=True)

	def test_already_in_requested_state_is_not_saved(self):
		file_doc = _file_doc("/private/files/id.jpg", is_private=1)
		with patch("frappe.db.get_value", return_value="F1"), patch("frappe.get_doc", return_value=file_doc):
			self.assertEqual(set_file_privacy("/private/files/id.jpg", private=True), "/private/files/id.jpg")
		file_doc.save.assert_not_called()

	def test_verify_attached_field_blanks_for_save_and_restores(self):
		"""Gallery-shaped row (attached_to_field="image"): the save runs with the
		field blanked so Frappe only rewrites a parent field that holds the URL;
		when Frappe finds none it saves None → restored via db_set, no modified bump."""
		file_doc = _file_doc("/private/files/gun.jpg", is_private=1)
		file_doc.attached_to_field = "image"
		seen = {}

		def _save(**kwargs):
			seen["field_during_save"] = file_doc.attached_to_field
			file_doc.file_url = "/files/gun.jpg"
			file_doc.attached_to_field = None  # Frappe found no parent field holding the URL
		file_doc.save.side_effect = _save
		with patch("frappe.db.get_value", return_value="F1"), patch("frappe.get_doc", return_value=file_doc):
			out = set_file_privacy("/private/files/gun.jpg", private=False, verify_attached_field=True)
		self.assertEqual(out, "/files/gun.jpg")
		self.assertIsNone(seen["field_during_save"])
		file_doc.db_set.assert_called_once_with("attached_to_field", "image", update_modified=False)

	def test_verify_attached_field_keeps_frappe_rediscovered_field(self):
		"""Cover-shaped row: Frappe rediscovers ``image`` from the parent's values
		and saves it back itself — nothing to restore."""
		file_doc = _file_doc("/private/files/cover.jpg", is_private=1)
		file_doc.attached_to_field = "image"

		def _save(**kwargs):
			file_doc.file_url = "/files/cover.jpg"
			file_doc.attached_to_field = "image"
		file_doc.save.side_effect = _save
		with patch("frappe.db.get_value", return_value="F1"), patch("frappe.get_doc", return_value=file_doc):
			set_file_privacy("/private/files/cover.jpg", private=False, verify_attached_field=True)
		file_doc.db_set.assert_not_called()

	def test_default_trusts_attached_field(self):
		"""ID-scan path: attached_to_field is left alone (single-field attachment)."""
		file_doc = _file_doc("/files/id.jpg", is_private=0)
		file_doc.attached_to_field = "id_document"
		seen = {}

		def _save(**kwargs):
			seen["field_during_save"] = file_doc.attached_to_field
			file_doc.file_url = "/private/files/id.jpg"
		file_doc.save.side_effect = _save
		with patch("frappe.db.get_value", return_value="F1"), patch("frappe.get_doc", return_value=file_doc):
			set_file_privacy("/files/id.jpg", private=True)
		self.assertEqual(seen["field_during_save"], "id_document")
		file_doc.db_set.assert_not_called()


class TestSetFilePrivacyOnDisk(FrappeTestCase):
	"""Real Item + real attached Files: with ``verify_attached_field=True`` the flip
	rewrites the cover only for the file the cover actually holds — a gallery-only
	file (same ``attached_to_field="image"``) leaves the cover alone.  Cleanup rides
	Frappe's ``after_rollback`` handlers plus an order-agnostic sweep registered
	after the flips (see ffl_woo_sync's TestPublicizeFileOnDisk for the why)."""

	def _private_file(self, file_name, *, item_name):
		file_doc = frappe.new_doc("File")
		file_doc.file_name = file_name
		file_doc.is_private = 1
		file_doc.content = f"file privacy {file_name}".encode()
		file_doc.attached_to_doctype = "Item"
		file_doc.attached_to_name = item_name
		file_doc.attached_to_field = "image"
		file_doc.insert(ignore_permissions=True)
		return file_doc

	def _register_sweep(self, file_name):
		import os

		private_path = frappe.get_site_path("private", "files", file_name)
		public_path = frappe.get_site_path("public", "files", file_name)

		def _sweep():
			if not os.path.exists(public_path) and os.path.exists(private_path):
				os.remove(private_path)
		frappe.db.after_rollback.add(_sweep)

	def test_cover_rewritten_gallery_only_file_leaves_cover_alone(self):
		token = frappe.generate_hash(length=8)
		item = frappe.get_doc({
			"doctype": "Item", "item_code": f"FILE-PRIV-{token}", "item_name": f"File privacy {token}",
			"item_group": "Products", "stock_uom": "Nos", "is_stock_item": 1,
		})
		item.flags.ignore_permissions = True
		item.insert()
		cover = self._private_file(f"fp-cover-{token}.txt", item_name=item.name)
		extra = self._private_file(f"fp-extra-{token}.txt", item_name=item.name)
		item.image = cover.file_url
		item.append("image_gallery", {"image": cover.file_url, "is_primary": 1})
		item.append("image_gallery", {"image": extra.file_url, "is_primary": 0})
		item.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Item", item.name, "image"), cover.file_url)

		try:
			# The cover's own file: Frappe rediscovers ``image`` and rewrites it.
			cover_url = set_file_privacy(cover.file_url, private=False, verify_attached_field=True)
			self.assertEqual(frappe.db.get_value("Item", item.name, "image"), cover_url)
			# The gallery-only file: no parent field holds it → cover untouched.
			extra_url = set_file_privacy(extra.file_url, private=False, verify_attached_field=True)
		finally:
			self._register_sweep(cover.file_name)
			self._register_sweep(extra.file_name)

		self.assertEqual(cover_url, f"/files/{cover.file_name}")
		self.assertEqual(extra_url, f"/files/{extra.file_name}")
		self.assertEqual(frappe.db.get_value("Item", item.name, "image"), cover_url)
		for file_doc in (cover, extra):
			self.assertEqual(
				frappe.db.get_value("File", file_doc.name, ["is_private", "attached_to_field"], as_dict=True),
				{"is_private": 0, "attached_to_field": "image"},
			)


class TestEnsurePrivateFile(FrappeTestCase):
	"""The Gov-ID wrapper keeps its lenient contract: unknown files pass through."""

	def test_blank_passthrough(self):
		self.assertIsNone(_ensure_private_file(None))
		self.assertEqual(_ensure_private_file(""), "")

	def test_missing_row_returns_input_unchanged(self):
		with patch("ffl_core.api.receive_goods.set_file_privacy", return_value=None) as flip:
			self.assertEqual(_ensure_private_file("/files/id.jpg"), "/files/id.jpg")
		flip.assert_called_once_with("/files/id.jpg", private=True)

	def test_delegates_and_returns_new_url(self):
		with patch("ffl_core.api.receive_goods.set_file_privacy", return_value="/private/files/id.jpg"):
			self.assertEqual(_ensure_private_file("/files/id.jpg"), "/private/files/id.jpg")
