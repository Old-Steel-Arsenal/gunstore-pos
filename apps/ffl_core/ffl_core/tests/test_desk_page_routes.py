"""Every Desk Page we ship must actually be reachable.

Frappe's client router resolves the first path segment against the DocType
slug map *before* it ever looks at Pages:

    // apps/frappe/frappe/public/js/frappe/router.js
    slug(name) { return name.toLowerCase().replace(/ /g, "-"); }
    this.routes[this.slug(doctype)] = { doctype: doctype };   // every DocType
    ...
    } else if (this.routes[route[0]]) {                       // DocType wins
        route = await this.set_doctype_route(route);          // -> List View
    }

So a Page whose name equals some DocType's slug is dead on arrival: visiting
/app/<slug> opens that DocType's List View and `frappe.pages['<slug>']` is
never even defined. The `consignment-out` Page sat behind the `Consignment
Out` DocType in exactly that way and never rendered once; it now ships as
`ship-consignment` (title still "Consignment Out").

This module also pins the four-piece registration contract, because
Page.load_assets derives the on-disk path from the Page *name* -- never from
the folder it happened to find the JSON in:

    # apps/frappe/frappe/core/doctype/page/page.py
    dir_path = os.path.join(module_path, "page", frappe.scrub(self.name))
    fpath    = os.path.join(path, scrub(self.name) + ".js")

so folder, filenames and `name` have to agree under frappe.scrub().
"""
from __future__ import annotations

import json
import os
import tempfile

import frappe
from frappe.tests.utils import FrappeTestCase


def _page_root() -> str:
	return frappe.get_app_path("ffl_core", "ffl_core", "page")


def _slug(name: str) -> str:
	"""Mirror of router.js slug() -- the function that builds the route map."""
	return name.lower().replace(" ", "-")


def _all_page_dirs(root: str | None = None) -> list[str]:
	"""Every subdirectory under page/ — the honest enumeration.

	The folder/filename naming contract must
	be asserted over THIS, never over dirs pre-filtered to those that already
	hold a <dir>/<dir>.json. Using the contract as its own discovery filter made
	a Page whose JSON is mis-named invisible to the one test meant to catch it.
	"""
	root = root or _page_root()
	return sorted(
		d
		for d in os.listdir(root)
		if os.path.isdir(os.path.join(root, d)) and d != "__pycache__"
	)


def _page_dirs(root: str | None = None) -> list[str]:
	"""Page dirs that already satisfy the <dir>/<dir>.json naming contract, and so
	are safe to read meta from. The collision and registration checks only make
	sense for a page whose route is derivable — a validly-named one; the naming
	contract itself is guarded by test_folder_and_filenames_match_the_scrubbed_
	page_name over _all_page_dirs(), which does NOT pre-filter this way.
	"""
	root = root or _page_root()
	return sorted(
		d
		for d in _all_page_dirs(root)
		if os.path.isfile(os.path.join(root, d, f"{d}.json"))
	)


def _page_meta(folder: str) -> dict:
	with open(os.path.join(_page_root(), folder, f"{folder}.json")) as f:
		return json.load(f)


class DeskPageRoutes(FrappeTestCase):
	"""Structural guards over ffl_core's Desk Pages."""

	def test_page_name_never_collides_with_a_doctype_slug(self):
		"""A Page named like a DocType's slug is unreachable -- List View wins."""
		by_slug = {_slug(dt): dt for dt in frappe.get_all("DocType", pluck="name")}
		for folder in _page_dirs():
			name = _page_meta(folder)["name"]
			with self.subTest(page=name):
				self.assertNotIn(
					name,
					by_slug,
					msg=(
						f"Desk Page {name!r} collides with the DocType "
						f"{by_slug.get(name)!r} (slug {name!r}). "
						f"/app/{name} will open that DocType's List View and "
						f"frappe.pages[{name!r}] will never be defined. "
						f"Rename the page."
					),
				)

	def test_folder_and_filenames_match_the_scrubbed_page_name(self):
		"""Page.load_assets looks under page/<scrub(name)>/<scrub(name)>.js.

		Enumerated over _all_page_dirs() — every directory — so a Page whose JSON
		is mis-named is caught right here rather than silently skipped."""
		for folder in _all_page_dirs():
			with self.subTest(page=folder):
				self.assertTrue(
					os.path.isfile(os.path.join(_page_root(), folder, f"{folder}.json")),
					msg=(
						f"{folder}: Page.load_assets resolves "
						f"page/{folder}/{folder}.json — a JSON under any other name is "
						f"unreachable, and was invisible to this test's old dir filter"
					),
				)
				meta = _page_meta(folder)
				self.assertEqual(
					meta["name"],
					meta["page_name"],
					msg=f"{folder}: name and page_name must agree",
				)
				self.assertEqual(
					frappe.scrub(meta["name"]),
					folder,
					msg=(
						f"{folder}: Page.load_assets resolves "
						f"page/{frappe.scrub(meta['name'])}/, not page/{folder}/"
					),
				)
				for ext in ("js", "py"):
					self.assertTrue(
						os.path.isfile(os.path.join(_page_root(), folder, f"{folder}.{ext}")),
						msg=f"{folder}: missing {folder}.{ext}",
					)

	def test_js_registers_the_page_under_its_own_name(self):
		"""frappe.pages['<name>'] is the key desk.js dispatches on."""
		for folder in _page_dirs():
			name = _page_meta(folder)["name"]
			with self.subTest(page=name):
				with open(os.path.join(_page_root(), folder, f"{folder}.js")) as f:
					js = f.read()
				self.assertIn(
					f"frappe.pages['{name}']",
					js,
					msg=f"{folder}.js must register frappe.pages['{name}']",
				)

	def test_folder_discovery_does_not_use_the_naming_contract_as_its_filter(self):
		"""The folder/filename naming
		contract must be asserted over an HONEST enumeration of page/ — every
		subdirectory — never over dirs pre-filtered to those that already satisfy
		it. Using the contract as its own discovery filter made a Page whose JSON
		is mis-named invisible to test_folder_and_filenames_match_the_scrubbed_
		page_name, the one test whose whole job is to catch exactly that.

		Proven on a scratch tree: a dir whose JSON is under the wrong name is
		still discovered by _all_page_dirs (so the contract test can fail on it),
		while _page_dirs — kept, filtered, for the checks that need a derivable
		route — is exactly where it used to vanish."""
		with tempfile.TemporaryDirectory() as tmp:
			os.makedirs(os.path.join(tmp, "misnamed"))
			with open(os.path.join(tmp, "misnamed", "wrong_name.json"), "w") as f:
				json.dump({"name": "Misnamed"}, f)
			os.makedirs(os.path.join(tmp, "__pycache__"))
			os.makedirs(os.path.join(tmp, "good"))
			with open(os.path.join(tmp, "good", "good.json"), "w") as f:
				json.dump({"name": "good", "page_name": "good"}, f)

			self.assertIn(
				"misnamed",
				_all_page_dirs(tmp),
				msg="a page dir must be discovered by directory, not by whether it "
				"already satisfies the <dir>/<dir>.json contract",
			)
			self.assertNotIn(
				"misnamed",
				_page_dirs(tmp),
				msg="_page_dirs stays filtered on purpose — which is precisely why the naming test cannot use it as its discovery",
			)
			self.assertNotIn("__pycache__", _all_page_dirs(tmp))
