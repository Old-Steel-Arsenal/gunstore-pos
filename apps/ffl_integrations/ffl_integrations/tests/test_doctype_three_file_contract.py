"""Every doctype directory must be a complete three-file set: JSON + __init__ +
controller module.

Why this is its own test: the stubbed harness never runs `bench migrate`, and Frappe
imports `<doctype>.<doctype>` for EVERY doctype at migrate time. A doctype with a JSON
but no controller `.py` passes every unit test and then hard-fails migrate with
`No module named ...` — which is exactly how `Distributor Listing Cost Range` shipped
and stopped the gate run at migrate.

A missing controller is structurally invisible to a stubbed frappe, so it is asserted
here directly against the filesystem, the same way column names are (docs/testing.md).
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import ffl_integrations

_DOCTYPE_ROOT = Path(ffl_integrations.__file__).resolve().parent / "ffl_integrations" / "doctype"


def _doctype_dirs():
	for child in sorted(_DOCTYPE_ROOT.iterdir()):
		if not child.is_dir() or child.name == "__pycache__":
			continue
		if not (child / f"{child.name}.json").exists():
			continue  # not a doctype dir (e.g. a stray package)
		yield child


class DoctypeThreeFileContract(unittest.TestCase):
	def test_the_doctype_root_was_found(self):
		self.assertTrue(_DOCTYPE_ROOT.is_dir(), _DOCTYPE_ROOT)
		self.assertTrue(list(_doctype_dirs()), "no doctype dirs discovered")

	def test_every_doctype_has_its_controller_module(self):
		"""The one migrate actually imports. Missing => `No module named ...` at
		`bench migrate`, unit-test-green the whole way there."""
		missing = [d.name for d in _doctype_dirs()
			if not (d / f"{d.name}.py").exists()]
		self.assertEqual(missing, [],
			f"doctype dirs with no controller .py (will fail bench migrate): {missing}")

	def test_every_doctype_has_its_init(self):
		missing = [d.name for d in _doctype_dirs()
			if not (d / "__init__.py").exists()]
		self.assertEqual(missing, [], f"doctype dirs with no __init__.py: {missing}")


if __name__ == "__main__":
	unittest.main()
