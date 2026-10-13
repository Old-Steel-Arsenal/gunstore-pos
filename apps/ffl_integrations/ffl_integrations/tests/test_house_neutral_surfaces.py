"""Generic distributor surfaces name no house: a Sports
South order must never read "RSR", and the next distributor must slot in without a
wording pass. House-specific words belong in that house's own module and Settings.

Checked: the labels / descriptions / options of the shared doctypes, the
translatable strings of the shared page + form scripts, and every string literal
(docstrings excepted) of the shared server modules. Doctype names such as
"RSR Settings" are data, not wording, and are allowed."""
from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

HOUSES = re.compile(r"\bRSR\b|Sports South")
APPS = Path(__file__).resolve().parents[3]
INTEG = APPS / "ffl_integrations" / "ffl_integrations"
CORE = APPS / "ffl_core" / "ffl_core"

DOCTYPES = [
	INTEG / "ffl_integrations/doctype/distributor_order/distributor_order.json",
	INTEG / "ffl_integrations/doctype/distributor_order_line/distributor_order_line.json",
	INTEG / "ffl_integrations/doctype/distributor/distributor.json",
]
SCRIPTS = [
	INTEG / "ffl_integrations/doctype/distributor_order/distributor_order.js",
	INTEG / "ffl_integrations/page/distributor_hub/distributor_hub.js",
	INTEG / "ffl_integrations/page/distributor_restock/distributor_restock.js",
	CORE / "ffl_core/page/pending_dispositions/pending_dispositions.js",
	CORE / "ffl_core/page/create_order/create_order.js",
	CORE / "ffl_core/page/receive_goods/receive_goods.js",
]
MODULES = [
	INTEG / "distributor/api.py",
	INTEG / "distributor/tasks.py",
	INTEG / "distributor/receive_bridge.py",
	INTEG / "distributor/base.py",
	INTEG / "distributor/lanes.py",
]
ALLOWED = re.compile(r"^(RSR|Sports South) Settings$")


def _py_strings(path: Path):
	tree = ast.parse(path.read_text())
	docstrings = set()
	for node in ast.walk(tree):
		if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
			body = getattr(node, "body", [])
			if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
					and isinstance(body[0].value.value, str):
				docstrings.add(id(body[0].value))
	for node in ast.walk(tree):
		if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
			yield node.lineno, node.value


class HouseNeutralSurfaces(unittest.TestCase):
	def test_shared_doctypes_name_no_house(self):
		hits = []
		for path in DOCTYPES:
			for f in json.loads(path.read_text())["fields"]:
				for key in ("label", "description", "options"):
					if HOUSES.search(f.get(key) or ""):
						hits.append(f"{path.name}:{f['fieldname']}.{key}")
		self.assertEqual(hits, [])

	def test_shared_scripts_translate_no_house(self):
		hits = []
		for path in SCRIPTS:
			if not path.exists():
				continue
			for n, line in enumerate(path.read_text().splitlines(), 1):
				if line.lstrip().startswith("//"):
					continue
				for m in re.finditer(r"__\(\s*(['\"])(.*?)\1", line):
					if HOUSES.search(m.group(2)):
						hits.append(f"{path.name}:{n}")
		self.assertEqual(hits, [])

	def test_shared_modules_message_no_house(self):
		hits = [f"{p.name}:{n}" for p in MODULES for n, s in _py_strings(p)
			if HOUSES.search(s) and not ALLOWED.match(s)]
		self.assertEqual(hits, [])
