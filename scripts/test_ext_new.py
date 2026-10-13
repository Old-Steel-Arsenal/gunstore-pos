#!/usr/bin/env python3
"""Unit tests for scripts/ext_new.py — the `make ext-new` skeleton generator.

deploy/prod/tests/ext_hello is pinned as this generator's output, so the smoke
fixture, the lint tests that copy it and the skeleton a developer starts from
can never drift apart.

Run: python3 scripts/test_ext_new.py   (stdlib unittest, no bench)
"""
from __future__ import annotations

import ast
import datetime
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ext_new  # noqa: E402

REPO = HERE.parent
EXT_HELLO = REPO / "deploy" / "prod" / "tests" / "ext_hello"
LINT = REPO / "apps" / "ffl_core" / "ffl_core" / "extensions" / "lint.py"   # its lint tests run ext_hello through it on the bench
WORKFLOW = REPO / ".github" / "workflows" / "_app-tests.yml"


def scrubs(source: str) -> list[str]:
	"""Every `re.sub(…, …, <name>.lower()…)` in ``source``, unparsed with the name blanked."""
	found = []
	for node in ast.walk(ast.parse(source)):
		if isinstance(node, ast.Call) and ast.unparse(node.func) == "re.sub" and len(node.args) == 3:
			base = node.args[2]
			while isinstance(base, (ast.Call, ast.Attribute)):
				base = base.func if isinstance(base, ast.Call) else base.value
			if isinstance(base, ast.Name) and ".lower()" in ast.unparse(node.args[2]):
				base.id = "NAME"
				found.append(ast.unparse(node))
	return found


def tree(root: Path) -> dict[str, bytes]:
	return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
		if p.is_file() and "__pycache__" not in p.parts}


class Generate(unittest.TestCase):
	def setUp(self):
		self._tmp = tempfile.TemporaryDirectory()
		self.tmp = Path(self._tmp.name)

	def tearDown(self):
		self._tmp.cleanup()

	def run_cli(self, *args: str) -> subprocess.CompletedProcess:
		return subprocess.run([sys.executable, str(HERE / "ext_new.py"), *args], capture_output=True, text=True)

	def test_ext_hello_is_the_generator_output(self):
		header = (EXT_HELLO / "ext_hello" / "change_log" / "v0" / "v0_1_0.md").read_text().splitlines()[0]
		date = datetime.datetime.strptime(header.split(" — ", 1)[1], "%B %d, %Y").date()
		out = self.tmp / "ext_hello"
		ext_new.generate("ext_hello", "Ext Hello", "Acme Software LLC", out, date)
		self.assertEqual(sorted(tree(out)), sorted(tree(EXT_HELLO)))
		for rel, data in tree(out).items():
			self.assertEqual(data, (EXT_HELLO / rel).read_bytes(),
				f"{rel} differs — regenerate: rm -rf {EXT_HELLO} && make ext-new ID=ext_hello "
				f'TITLE="Ext Hello" PUBLISHER="Acme Software LLC" OUT={EXT_HELLO}')

	def test_every_placeholder_is_filled(self):
		out = self.tmp / "acme_layaway"
		result = self.run_cli("--id", "acme_layaway", "--title", "Acme Layaway Plans", "--publisher", "Acme Software LLC",
			"--out", str(out), "--date", "2026-02-03")
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertIn(f"make ext-test ID=acme_layaway DIR={out}", result.stdout)
		files = tree(out)
		self.assertFalse([rel for rel, data in files.items() if b"{{" in data or "{{" in rel])
		for rel in ("pyproject.toml", "README.md", "LICENSE", ".gitignore", "acme_layaway/__init__.py",
				"acme_layaway/hooks.py", "acme_layaway/modules.txt", "acme_layaway/acme_layaway_plans/__init__.py",
				"acme_layaway/api/greet.py", "acme_layaway/hooks_impl/customer.py",
				"acme_layaway/public/js/acme_layaway.bundle.js", "acme_layaway/change_log/v0/v0_1_0.md",
				"acme_layaway/tests/test_greet.py", "docs/api/openapi.json", "docs/api/reference/README.md",
				"docs/api/reference/acme_layaway/api.greet.md"):
			self.assertIn(rel, files)
		hooks = {n.targets[0].id: ast.literal_eval(n.value) for n in ast.parse(files["acme_layaway/hooks.py"]).body
			if isinstance(n, ast.Assign)}
		self.assertEqual((hooks["app_name"], hooks["app_title"], hooks["app_publisher"]),
			("acme_layaway", "Acme Layaway Plans", "Acme Software LLC"))
		manifest = hooks["gunshop_extension"]
		self.assertEqual((manifest["id"], manifest["publisher_id"], manifest["version"]), ("acme_layaway", "acme", "0.1.0"))
		self.assertEqual(files["acme_layaway/modules.txt"], b"Acme Layaway Plans\n")
		self.assertTrue(files["acme_layaway/change_log/v0/v0_1_0.md"].startswith(b"### Version 0.1.0 \xe2\x80\x94 February 3, 2026\n"))
		self.assertIn(b"Copyright (c) 2026 Acme Software LLC", files["LICENSE"])
		for rel, data in files.items():
			if rel.endswith(".py"):
				compile(data, rel, "exec")

	def test_bad_input_is_refused_and_nothing_is_written(self):
		taken = self.tmp / "taken"
		(taken / "x").mkdir(parents=True)
		for args in (
			("--id", "Acme_hello"), ("--id", "a_hello"), ("--id", "acmehello"), ("--id", "acme_hello\n"),
			("--title", "A title that is much longer than thirty"), ("--title", 'Say "hi"'), ("--title", "Acme\tHello"),
			("--publisher", 'Acme "Software"'), ("--publisher", " "), ("--publisher", "Acme\rSoftware"),
			("--publisher", "Acme\x7fSoftware"), ("--publisher", "Acme\x85Software"),
			("--out", str(taken)), ("--date", "13/09/2026"),
		):
			base = {"--id": "acme_hello", "--title": "Acme Hello", "--publisher": "Acme", "--out": str(self.tmp / "new")}
			base.update(dict([args]))
			result = self.run_cli(*[x for kv in base.items() for x in kv])
			self.assertEqual(result.returncode, 2, (args, result.stdout, result.stderr))
			self.assertTrue(result.stderr.strip(), args)
			self.assertFalse((self.tmp / "new").exists(), args)
		self.assertEqual([p.name for p in taken.iterdir()], ["x"])

	def test_a_failed_generation_leaves_nothing_behind(self):
		failure = subprocess.CalledProcessError(1, "generate_api_docs.py")
		for existed in (False, True):
			out = self.tmp / f"out-{existed}"
			if existed:
				out.mkdir()
			with mock.patch.object(ext_new.subprocess, "run", side_effect=failure), self.assertRaises(subprocess.CalledProcessError):
				ext_new.generate("acme_hello", "Acme Hello", "Acme", out, datetime.date(2026, 9, 13))
			self.assertEqual(sorted(out.iterdir()) if out.exists() else None, [] if existed else None)
		with mock.patch.object(ext_new.subprocess, "run", side_effect=failure):
			self.assertEqual(ext_new.main(["--id", "acme_hello", "--title", "Acme Hello", "--publisher", "Acme",
				"--out", str(self.tmp / "via-main")]), 1)
		self.assertFalse((self.tmp / "via-main").exists())

	def test_id_rule_and_module_scrub_are_the_lints(self):
		lint_source = LINT.read_text()
		lint_id_re = next(n.value for n in ast.parse(lint_source).body
			if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "ID_RE")
		pattern = ast.literal_eval(lint_id_re.args[0])
		self.assertEqual(ext_new.ID_RE.pattern, pattern)
		# The CI fetch step checks the listed ids before any bench (and so ffl_core) exists.
		ci_patterns = re.findall(r're\.fullmatch\(r"([^"]+)", e\["id"\]\)', WORKFLOW.read_text())
		self.assertEqual([f"^{p}$" for p in ci_patterns], [pattern])
		# The module package: frappe.scrub(title), the way the lint derives it.
		(ours,) = scrubs((HERE / "ext_new.py").read_text())
		self.assertIn(ours, scrubs(lint_source))


if __name__ == "__main__":
	unittest.main(verbosity=1)
