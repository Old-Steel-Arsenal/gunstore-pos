"""The Pending Order page's three-source card, actually EXECUTED.

The page grew a third order source, and what made that dangerous does not show
up in a diff. Every capability on the card used to be written ``!isWeb``, which
quietly meant "counter" — so a GunBroker row coming through the same loop would
have been offered Record Payment, Edit and Cancel Order. Cancel Order on a
GunBroker sale is not a mistake that announces itself: GunBroker gives a seller
no way to unwind a sale, so the button would reverse our ledger against a sale
that still exists, with a buyer who has the gun.

The harness runs the page inside ``node:vm`` against a stub Desk and asserts the
capability table per source, both regressions (the counter and web button sets
must be exactly what they were), that a GunBroker card renders no ``undefined``
— its row contract has around fifty keys and the page reads all of them
unconditionally — and that Dispose dispatches to the right backend for each of
the three.

Verified by injecting the real defect: putting ``!isWeb`` back on the Cancel
Order button turns the third check red with the reason spelled out. It is not a
DOM test and does not pretend to be one; it is the cheapest thing that makes
"this page has never been run with a GunBroker row in it" a false statement.

Node is present in every bench environment (bench builds Desk assets with it);
if it somehow is not, the test SKIPS rather than failing on infrastructure.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import ffl_core
import ffl_integrations

_INT_ROOT = Path(ffl_integrations.__file__).resolve().parent
_CORE_ROOT = Path(ffl_core.__file__).resolve().parent
_HARNESS = _INT_ROOT / "tests" / "js" / "pending_orders_page.harness.js"
_PAGE_JS = (_CORE_ROOT / "ffl_core" / "page" / "pending_dispositions"
	/ "pending_dispositions.js")


class TestPendingOrdersPageJS(unittest.TestCase):
	def test_three_sources_render_with_the_right_buttons(self):
		node = shutil.which("node")
		if not node:
			self.skipTest("node not on PATH — cannot execute the page JS")
		self.assertTrue(_HARNESS.exists(), f"harness missing: {_HARNESS}")
		self.assertTrue(_PAGE_JS.exists(), f"page missing: {_PAGE_JS}")

		proc = subprocess.run([node, str(_HARNESS), str(_PAGE_JS)],
			capture_output=True, text=True, timeout=60, check=False)
		# A crash outside the checks (syntax error, bad harness) leaves no JSON —
		# surface stderr rather than a confusing JSONDecodeError.
		self.assertTrue(proc.stdout.strip(),
			f"harness produced no output.\nstderr:\n{proc.stderr}")
		report = json.loads(proc.stdout)
		failures = [c for c in report["checks"] if not c["ok"]]
		self.assertEqual(failures, [], "page JS failed to execute:\n" + "\n".join(
			f"  - {c['name']}: {c['error']}" for c in failures))
		self.assertEqual(proc.returncode, 0, proc.stderr)
		# Guard against a harness that silently stops asserting anything.
		self.assertGreaterEqual(len(report["checks"]), 40)
