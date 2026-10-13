"""No store identity or address in the tracked tree.

The product is meant to be public. A store's name, its street address or a
personal handle that slips into a test fixture, a comment or a doc travels with
every clone. This walks ``git ls-files`` and fails on any hit of the public terms
in RESIDUE below.

Private identifiers (licence numbers, vendor account, order and PO numbers) are
checked outside this repo: writing them down here, even hashed, would publish
what the guard protects.

Run it two ways:

* under the bench (``make test``), as an ordinary test;
* with plain Python, no bench and no Frappe, from the repo root:
  ``python3 apps/ffl_core/ffl_core/tests/test_no_store_residue.py --standalone``
  (the api-docs job in release-image.yml does this on every PR).

Exceptions are exact and visible in ``store_residue_allowlist.txt`` next to this
file, one per line: ``path :: allowed substring  # reason``. ``path`` may be a glob
(``fnmatch``); the substring is blanked out of a line before it is matched, so it
allows exactly that text and nothing else on the line. At most MAX_ALLOWLIST
entries; an entry that no longer allows anything fails, so the list only shrinks.
The only paths never read are SKIP_PREFIXES/SKIP_NAMES: legal text, release notes,
vendored assets and the two guard files.

Fixtures use ``_Test Store A/B``, abbreviations ``TSA/TSB``, ``example.com``,
555 phone numbers and the ``9-99-999-...`` / ``1-99-888-...`` licence pattern.
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
import unittest
from pathlib import Path

MAX_ALLOWLIST = 15

# PUBLIC terms only; private identifiers are checked outside this repo.
# label -> case-insensitive regex; every one is bounded, so that
# "all alphanumeric" is not "all alpha" and a longer word is not "plano".
RESIDUE: dict[str, str] = {
	"old steel arsenal": r"\bold steel arsenal\b",
	"oldsteelarsenal": r"\boldsteelarsenal\b",
	"caligunsandammo": r"\bcaligunsandammo\b",
	"california guns": r"\bcalifornia guns\b",
	"all alpha": r"\ball ?alpha\b",
	"aaarange": r"\baaarange\b",
	"allalphaarmory": r"\ballalphaarmory\b",
	"3209 premier": r"\b3209 premier\b",
	"plano": r"\bplano\b",
	"75075": r"\b75075\b",
	"xuanji86": r"\bxuanji86\b",
	"osa-logo": r"\bosa-logo\b",
}

# Never read: legal text, release notes, vendored assets and the guard's own files.
SKIP_PREFIXES = (
	"docs/api/assets/",
	"apps/ffl_core/ffl_core/change_log/",
	"apps/ffl_core/ffl_core/tests/test_no_store_residue.py",
	"apps/ffl_core/ffl_core/tests/store_residue_allowlist.txt",
)
SKIP_NAMES = ("LICENSE",)

HERE = Path(__file__).resolve()
ALLOWLIST = HERE.with_name("store_residue_allowlist.txt")


def repo_root() -> Path:
	# apps/ffl_core/ffl_core/tests/<this file>
	return HERE.parents[4]


def tracked_files(root: Path) -> list[str] | None:
	"""``git ls-files``, or None when git cannot answer (e.g. inside a container
	that mounts a linked worktree without its git directory)."""
	try:
		out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
	except (OSError, subprocess.CalledProcessError):
		return None
	return [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]


def read_allowlist() -> list[tuple[str, str, str]]:
	"""(path glob, allowed substring, reason)."""
	entries = []
	for raw in ALLOWLIST.read_text(encoding="utf-8").splitlines():
		line = raw.strip()
		if not line or line.startswith("#"):
			continue
		spec, _, reason = line.partition("  #")
		path, sep, allowed = spec.partition(" :: ")
		if not sep:
			raise ValueError(f"allowlist line needs 'path :: substring': {raw!r}")
		entries.append((path.strip(), allowed.strip(), reason.strip()))
	return entries


def line_hits(line: str) -> list[str]:
	"""Labels of every residue term on one line."""
	return [label for label, rx in _COMPILED.items() if rx.search(line)]


_COMPILED = {label: re.compile(rx, re.IGNORECASE) for label, rx in RESIDUE.items()}


def find_residue(root: Path, files: list[str], allow=()):
	"""Returns (hits, used): hits are (path, label, line number, excerpt) outside the
	skips and the allowlist; used is the set of allowlist entries that allowed something."""
	hits, used = set(), set()
	for rel in files:
		if rel.startswith(SKIP_PREFIXES) or Path(rel).name in SKIP_NAMES:
			continue
		mine = [(i, e) for i, e in enumerate(allow) if fnmatch.fnmatch(rel, e[0])]
		# The file's own name counts as line 0: a bundled asset can carry a store name.
		scanned = [(0, rel)]
		try:
			data = (root / rel).read_bytes()
		except OSError:
			data = b""
		if b"\0" not in data:
			try:
				scanned += list(enumerate(data.decode("utf-8").splitlines(), 1))
			except UnicodeDecodeError:
				pass
		for number, line in scanned:
			if not line_hits(line):
				continue
			rest = line
			for i, (_, allowed, _) in mine:
				if allowed in rest:
					blanked = rest.replace(allowed, " " * len(allowed))
					if set(line_hits(blanked)) != set(line_hits(rest)):
						used.add(i)
					rest = blanked
			for label in line_hits(rest):
				hits.add((rel, label, number, line.strip()[:120]))
	return hits, used


def check(root: Path | None = None) -> list[str]:
	"""Problems, as printable lines; empty means the tree is clean."""
	root = root or repo_root()
	files = tracked_files(root)
	if files is None:
		raise RuntimeError("git ls-files is not available here")
	problems = []
	allow = read_allowlist()
	if len(allow) > MAX_ALLOWLIST:
		problems.append(f"allowlist has {len(allow)} entries; the limit is {MAX_ALLOWLIST}")
	for path, allowed, reason in allow:
		if not reason:
			problems.append(f"allowlist entry {path} :: {allowed} has no reason")
	hits, used = find_residue(root, files, allow)
	for i, (path, allowed, _) in enumerate(allow):
		if i not in used:
			problems.append(f"stale allowlist entry {path} :: {allowed} (it allows nothing now)")
	for path, label, number, excerpt in sorted(hits):
		problems.append(f"{path}:{number}: {label!r}: {excerpt}")
	return problems


class NoStoreResidue(unittest.TestCase):
	def test_the_tracked_tree_carries_no_store_residue(self):
		try:
			problems = check()
		except RuntimeError as exc:
			self.skipTest(str(exc))
		self.assertEqual(problems, [], "\n" + "\n".join(problems))

	def _scan(self, files: dict[str, str], allow=()):
		import tempfile

		with tempfile.TemporaryDirectory() as tmp:
			root = Path(tmp)
			for name, text in files.items():
				path = root / name
				path.parent.mkdir(parents=True, exist_ok=True)
				path.write_text(text)
			hits, used = find_residue(root, list(files), list(allow))
		return {(p, label) for p, label, _, _ in hits}, used

	def test_the_guard_bites(self):
		"""Planted public terms are found, and the structural skips stay skipped."""
		found, _ = self._scan({
			"a.py": "x = 'Visit Old Steel Arsenal at 3209 Premier Drive'\n",
			"LICENSE": "Old Steel Arsenal\n",
		})
		self.assertEqual(found, {("a.py", "old steel arsenal"), ("a.py", "3209 premier")})

	def test_an_app_email_line_is_not_a_free_pass(self):
		"""The line markers are gone: only an exact allowlisted substring is allowed."""
		line = 'app_email = "plano@example.com"  # Old Steel Arsenal\n'
		found, _ = self._scan({"hooks.py": line})
		self.assertEqual(found, {("hooks.py", "plano"), ("hooks.py", "old steel arsenal")})
		contact = 'app_email = "info@' + "oldsteelarsenal" + '.com"\n'
		allow = [("hooks.py", "info@" + "oldsteelarsenal" + ".com", "the project contact")]
		found, used = self._scan({"hooks.py": contact}, allow)
		self.assertEqual((found, used), (set(), {0}))
		# The allowed text is allowed; a second term on the same line is not.
		found, _ = self._scan({"hooks.py": contact.rstrip() + "  # plano\n"}, allow)
		self.assertEqual(found, {("hooks.py", "plano")})

	def test_terms_have_word_boundaries(self):
		found, _ = self._scan({"a.md": "all alphanumeric, a planogram, the explanation, 75075" "1\n"})
		self.assertEqual(found, set())
		found, _ = self._scan({"b.md": "All Alpha. ALLALPHA arms. Plano, TX 75075\n"})
		self.assertEqual({label for _, label in found}, {"all alpha", "plano", "75075"})

	def test_a_store_name_in_a_file_name_is_found(self):
		found, _ = self._scan({"images/osa-logo.png": "x"})
		self.assertEqual(found, {("images/osa-logo.png", "osa-logo")})

	def test_this_file_and_its_allowlist_hold_no_private_identifier(self):
		"""The guard must not publish what it guards: no licence-shaped number and no
		long digit run (account, order, PO) in either file."""
		licence = re.compile(r"\d-\d{2}-\d{3}-\d{2}-[0-9A-Za-z]{2}-\d{5}")
		digits = re.compile(r"\d{6,}")
		for path in (HERE, ALLOWLIST):
			for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
				self.assertIsNone(licence.search(line) or digits.search(line), f"{path.name}:{number}")

	def test_the_allowlist_is_short_justified_and_exact(self):
		allow = read_allowlist()
		self.assertLessEqual(len(allow), MAX_ALLOWLIST)
		for path, allowed, reason in allow:
			self.assertTrue(allowed and reason, f"{path}: needs a substring and a reason")


if __name__ == "__main__":
	if "--standalone" in sys.argv:
		try:
			found = check()
		except RuntimeError as exc:
			print(f"cannot run: {exc}", file=sys.stderr)
			sys.exit(2)
		for line in found:
			print(line)
		print(f"store residue guard: {'FAILED, ' + str(len(found)) + ' problem(s)' if found else 'clean'}")
		sys.exit(1 if found else 0)
	unittest.main()
