"""The delist buttons must read the delist sentinel, not just the product id.

``delist_serial`` and ``delist_item`` deliberately KEEP ``{site}_product_id``:
the WooCommerce product still exists, drafted, and that id is how it is found
again to relist. The field that records "this is currently delisted" is
``{site}_delisted``.

Both form scripts gated the red Delist button on the product id alone, so after
a delist the button stayed. Nothing on the form said the gun was already off the
shop, and pressing it again sent a second live WooCommerce call to draft an
already-drafted product, behind a confirm dialog implying something would
change. Reported from the shop floor on 2026-09-03.

These assert on the JavaScript source, which is how this repo pins desk-script
behaviour it cannot exercise from Python (see
``ffl_core.tests.test_field_descriptions``). Every gate is checked in both
directions: the sentinel has to be consulted, and the bare product-id gate must
not come back.
"""
from __future__ import annotations

import re
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

# (file, product-id field, sentinel field) for each of the two buttons.
_GATES = (
	("serial_no_woo.js", "woo_product_id", "woo_delisted"),
	("item_woo.js", "woo_product_id", "woo_delisted"),
)


def _source(filename: str) -> str:
	path = frappe.get_app_path("ffl_woo_sync", "public", "js", filename)
	with open(path, encoding="utf-8") as fh:
		return fh.read()


class DelistButtonReadsTheSentinel(FrappeTestCase):
	def test_every_delist_gate_consults_the_delist_flag(self):
		for filename, product_field, sentinel in _GATES:
			source = _source(filename)
			gate = re.search(
				r"if\s*\(\s*frm\.doc\." + re.escape(product_field) + r"\b[^)]*\)",
				source,
			)
			self.assertIsNotNone(
				gate, f"{filename}: no gate found on frm.doc.{product_field}")
			self.assertIn(
				sentinel, gate.group(0),
				f"{filename}: the {product_field} gate does not consult "
				f"{sentinel}, so the Delist button survives a delist")

	def test_no_gate_is_left_on_the_product_id_alone(self):
		"""The regression this file exists for. A gate that tests only the
		product id is the bug, whatever else the file says elsewhere."""
		bare = re.compile(r"if\s*\(\s*frm\.doc\.woo_product_id\s*\)")
		for filename in ("serial_no_woo.js", "item_woo.js"):
			found = bare.search(_source(filename))
			self.assertIsNone(
				found,
				f"{filename}: {found.group(0) if found else ''!r} gates a "
				"Delist button on the product id alone; a delisted record keeps "
				"its product id, so the button never goes away")

	def test_the_delisted_state_is_shown_and_not_merely_implied(self):
		"""Hiding the button is not the same as saying why it is gone.

		Encoding state in the presence of a control is what produced this bug in
		the first place, so each script has to state the delisted state outright.
		"""
		for filename in ("serial_no_woo.js", "item_woo.js"):
			source = _source(filename)
			self.assertRegex(
				source, r"add_indicator\(",
				f"{filename}: nothing tells the operator the record is "
				"delisted; add a form indicator alongside hiding the button")

	def test_the_header_comment_matches_the_gate(self):
		"""An earlier change shipped a batch of comments that described behaviour the code
		did not have. Not repeating that here."""
		for filename in ("serial_no_woo.js", "item_woo.js"):
			header = _source(filename).split("*/", 1)[0]
			self.assertNotRegex(
				header, r"[Oo]nly (shown )?when \w*woo_product_id is set",
				f"{filename}: the header still describes the old gate")


class TheSentinelFieldsExist(FrappeTestCase):
	"""If a rename ever lands, the gates above would silently read undefined and
	the button would come back. Fail here instead, where the message says why."""

	def test_the_delist_flag_is_on_both_doctypes(self):
		for doctype in ("Serial No", "Item"):
			self.assertTrue(
				frappe.get_meta(doctype).has_field("woo_delisted"),
				f"{doctype}.woo_delisted is gone; the delist buttons read it")


if __name__ == "__main__":
	unittest.main()
