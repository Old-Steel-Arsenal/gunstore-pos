"""rsr/search.py — legacy RPC shim over RSRProvider.search (Task 3 follow-up).

ffl_core.bundle.js's shared `ffl.FirearmItemPicker` (Receive Goods pickup
dialog + Trade-in item picker — verified by reading both instantiation sites
and the class's `_render`/`_openPromote` methods) calls the whitelisted
`ffl_integrations.rsr.search.search` RPC by dotted path and reads these legacy
RSR Catalog Item column names straight off each hit: rsr_stock_number, upc,
manufacturer, model, short_description, department_number, dealer_price,
map_price, msrp, ffl_required, image_url. `mfg_number`, `status_flag` and
`image_url_hires` are NOT read anywhere in that class — confirmed by grepping
the whole bundle, not assumed — so the shim empty-strings them rather than
fetching extra data nothing on the page uses.
"""
from __future__ import annotations

from frappe.tests.utils import FrappeTestCase

from ffl_integrations.rsr import search
from ffl_integrations.tests import catalog_stub


class TestRsrSearchShim(FrappeTestCase):
	def setUp(self):
		self.stub = catalog_stub.install()
		catalog_stub.seed_catalog([
			{"rsr_stock_number": "GLK19", "upc": "764503037082", "dealer_price": 445.5,
			 "map_price": 499.0, "msrp": 599.0, "quantity": 12,
			 "department_number": "01", "ffl_required": 1, "model": "19",
			 "short_description": "GLOCK 19 GEN5", "manufacturer": "GLOCK",
			 "image_url": "http://x/glk19.jpg"},
			{"rsr_stock_number": "DEADSKU", "status_flag": "Deleted",
			 "short_description": "GLOCK OLD", "manufacturer": "GLOCK"},
		])

	def tearDown(self):
		catalog_stub.uninstall()

	def test_seeded_row_round_trips_with_legacy_keys(self):
		hits = search.search("GLOCK", 10)
		self.assertEqual(len(hits), 1)
		row = hits[0]
		self.assertEqual(row["rsr_stock_number"], "GLK19")
		self.assertEqual(row["upc"], "764503037082")
		self.assertEqual(row["manufacturer"], "GLOCK")
		self.assertEqual(row["model"], "19")
		self.assertEqual(row["short_description"], "GLOCK 19 GEN5")
		self.assertEqual(row["department_number"], "01")
		self.assertEqual(row["dealer_price"], 445.5)
		self.assertEqual(row["map_price"], 499.0)
		self.assertEqual(row["msrp"], 599.0)
		self.assertEqual(row["quantity"], 12)
		self.assertEqual(row["ffl_required"], True)
		self.assertEqual(row["image_url"], "http://x/glk19.jpg")
		# The provider's search-row shape does not carry these — the JS never
		# reads them off an RSR-sourced row (verified against the FirearmItemPicker
		# class in ffl_core.bundle.js), kept present-but-empty for shape stability.
		self.assertEqual(row["mfg_number"], "")
		self.assertEqual(row["status_flag"], "")
		self.assertEqual(row["image_url_hires"], "")

	def test_empty_query_is_empty_list(self):
		self.assertEqual(search.search("", 10), [])
		self.assertEqual(search.search("   ", 10), [])

	def test_deleted_row_is_excluded(self):
		"""The provider search already excludes Deleted rows (stub mirrors the
		real service); the shim must not re-admit them."""
		self.assertEqual(search.search("GLOCK OLD", 10), [])
