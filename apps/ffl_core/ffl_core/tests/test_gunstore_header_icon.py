import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.patches import v0_1_2_gunstore_header_icon as patch
from ffl_core.setup.gunshop_sidebar import mirror_shells
from ffl_core.setup.sidebar import SIDEBAR_NAME, gun_shop_items


class TestGunstoreHeaderIcon(FrappeTestCase):
	def setUp(self):
		frappe.db.savepoint("hdr_icon")
		self.addCleanup(frappe.db.rollback, save_point="hdr_icon")
		self.addCleanup(frappe.clear_cache, doctype="Sidebar")

	def _icon(self, name):
		return frappe.db.get_value("Sidebar", name, "header_icon")

	def test_the_gun_shop_sidebar_gets_the_mark_and_unrelated_sidebars_keep_retail(self):
		mirrors = set(mirror_shells(gun_shop_items(), "FFL Core"))
		other = frappe.get_all("Sidebar", filters={"name": ["!=", SIDEBAR_NAME],
			"module": ["not in", list(mirrors) or [""]]}, pluck="name", limit=1)
		for name in [SIDEBAR_NAME] + other:
			frappe.db.set_value("Sidebar", name, "header_icon", "retail", update_modified=False)

		patch.execute()
		patch.execute()  # idempotent

		self.assertEqual(self._icon(SIDEBAR_NAME), "gunstore")
		for name in other:
			self.assertEqual(self._icon(name), "retail")

	def test_an_icon_the_store_chose_is_left_alone(self):
		frappe.db.set_value("Sidebar", SIDEBAR_NAME, "header_icon", "star", update_modified=False)
		patch.execute()
		self.assertEqual(self._icon(SIDEBAR_NAME), "star")

	def test_the_sprite_is_registered_and_defines_the_icon(self):
		import os

		self.assertIn("/assets/ffl_core/icons/gunstore.svg", frappe.get_hooks("app_include_icons"))
		path = os.path.join(frappe.get_app_path("ffl_core"), "public", "icons", "gunstore.svg")
		with open(path) as f:
			self.assertIn('id="icon-gunstore"', f.read())
