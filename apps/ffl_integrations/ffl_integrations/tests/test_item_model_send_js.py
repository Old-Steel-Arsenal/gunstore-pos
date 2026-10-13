"""The Item form's GunBroker buttons and the model Send dialog, actually EXECUTED (R3-26).

A uniform-price gun lists by model from its Item form: a Send button only for that
kind of Item, and a dialog that shows what the listing would carry and what
GunBroker's paid promotions cost before anyone ticks one. None of that shows up as
an error when it is wrong — a Send button on a gun priced one by one can only come
back refused, a missing one is a model nobody can list, and a dialog that drops the
cost note has people buying extra features without being told they are billed.

The harness runs ``public/js/item.js`` inside ``node:vm`` against a stub Desk. It is
not a DOM test; it asserts the buttons drawn per kind of Item, the dialog fields and
text, and the calls the dialog makes.

Node is present in every bench environment (bench builds Desk assets with it); if it
somehow is not, the test SKIPS rather than failing on infrastructure.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import ffl_integrations

_INT_ROOT = Path(ffl_integrations.__file__).resolve().parent
_HARNESS = _INT_ROOT / "tests" / "js" / "item_model_send.harness.js"
_ITEM_JS = _INT_ROOT / "public" / "js" / "item.js"


class TestItemFormModelSendJS(unittest.TestCase):
	def test_the_item_form_draws_the_right_buttons_and_the_model_dialog(self):
		node = shutil.which("node")
		if not node:
			self.skipTest("node not on PATH — cannot execute the form JS")
		self.assertTrue(_HARNESS.exists(), f"harness missing: {_HARNESS}")
		self.assertTrue(_ITEM_JS.exists(), f"form script missing: {_ITEM_JS}")

		proc = subprocess.run([node, str(_HARNESS), str(_ITEM_JS)],
			capture_output=True, text=True, timeout=60, check=False)
		self.assertTrue(proc.stdout.strip(),
			f"harness produced no output.\nstderr:\n{proc.stderr}")
		report = json.loads(proc.stdout)
		failures = [c for c in report["checks"] if not c["ok"]]
		self.assertEqual(failures, [], "item form JS failed to execute:\n" + "\n".join(
			f"  - {c['name']}: {c['error']}" for c in failures))
		self.assertEqual(proc.returncode, 0, proc.stderr)
		# Guard against a harness that silently stops asserting anything.
		self.assertGreaterEqual(len(report["checks"]), 13)
