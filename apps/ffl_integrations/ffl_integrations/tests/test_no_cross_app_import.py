"""The two channel apps must never import each other.

`ffl_integrations` owns the GunBroker channel; `ffl_woo_sync` owns the web
store. Anything they genuinely share lives in `ffl_core`, which both may
import. The rule exists because the alternative is not hypothetical: the web
store's notion of a "site" is a hard-coded two-valued thing (retail / dealer)
whose lookup raises on anything else, deliberately, so that a third channel
fails closed instead of half-working. Reaching across would produce a channel
that looks integrated and is not — and it would make either app impossible to
install without the other, on a site that only wants one.

Asserted statically, against the source, because an import that only happens
inside a rarely-taken branch would not show up in any test run: a
`from ffl_woo_sync…` buried in an exception handler is exactly the shape that
gets merged.

Both directions are checked from this one file, and the ffl_woo_sync half is
checked here rather than in that app's own tests on purpose — a single place
means the rule cannot be half-deleted.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

import ffl_integrations

_INT_ROOT = Path(ffl_integrations.__file__).resolve().parent
_APPS_ROOT = _INT_ROOT.parent.parent
_WOO_ROOT = _APPS_ROOT / "ffl_woo_sync" / "ffl_woo_sync"


def _imported_modules(path: Path) -> set[str]:
	"""Every module name this file imports, including inside functions.

	Walks the whole tree rather than just `tree.body`: a deferred import is
	still an import, and deferring one is the usual way a cross-app dependency
	gets in ("it's only used in this one branch").
	"""
	out: set[str] = set()
	tree = ast.parse(path.read_text(encoding="utf-8"))
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			out.update(alias.name for alias in node.names)
		elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
			out.add(node.module)
	return out


def _offenders(root: Path, forbidden_prefix: str) -> list[str]:
	if not root.is_dir():
		return []
	found: list[str] = []
	for path in sorted(root.rglob("*.py")):
		# Tests are allowed to reach across: a cross-app contract test has to be
		# able to name the other app to check that the wiring between them exists.
		if "tests" in path.parts or path.name.startswith("test_"):
			continue
		for module in _imported_modules(path):
			if module == forbidden_prefix or module.startswith(forbidden_prefix + "."):
				found.append(f"{path.relative_to(root.parent)} imports {module}")
	return found


class NoCrossAppImports(unittest.TestCase):
	def test_the_roots_were_found(self):
		"""A path typo would make every assertion below vacuously true."""
		self.assertTrue(_INT_ROOT.is_dir(), _INT_ROOT)
		self.assertTrue(list(_INT_ROOT.rglob("*.py")), "no ffl_integrations sources found")

	def test_ffl_integrations_never_imports_ffl_woo_sync(self):
		offenders = _offenders(_INT_ROOT, "ffl_woo_sync")
		self.assertEqual(offenders, [], "\n".join([
			"ffl_integrations must not import ffl_woo_sync.",
			"Put anything the two channels share in ffl_core, or invert the call",
			"with a hook the other app registers against.", *offenders]))

	def test_ffl_woo_sync_never_imports_ffl_integrations(self):
		if not _WOO_ROOT.is_dir():
			self.skipTest("ffl_woo_sync is not installed in this bench")
		offenders = _offenders(_WOO_ROOT, "ffl_integrations")
		self.assertEqual(offenders, [], "\n".join([
			"ffl_woo_sync must not import ffl_integrations.", *offenders]))

	def test_the_gunbroker_package_reaches_only_ffl_core(self):
		"""The narrower rule, stated where it is easiest to break.

		The GunBroker package is the newest reason somebody would want to reach
		into the web store's code — the two channels do similar things — so it
		gets its own assertion rather than relying on the app-wide one above to
		catch it among everything else.
		"""
		gunbroker = _INT_ROOT / "gunbroker"
		self.assertTrue(gunbroker.is_dir(), gunbroker)
		offenders = []
		for path in sorted(gunbroker.rglob("*.py")):
			for module in _imported_modules(path):
				if module.split(".")[0] == "ffl_woo_sync":
					offenders.append(f"{path.name} imports {module}")
		self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
	unittest.main()
