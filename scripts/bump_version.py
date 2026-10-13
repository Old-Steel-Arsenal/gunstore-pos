#!/usr/bin/env python3
"""Set the product version in all three apps — `make bump-version VERSION=x.y.z`.

The three custom apps share one product version (ffl_core is the source of
truth, the other two mirror it; `ffl_core.tests.test_changelog` asserts they
agree). A release is not bumpable until its release note exists:
`apps/ffl_core/ffl_core/change_log/v<major>/v<x>_<y>_<z>.md` — that is the
file the login popup and Help → What's New render. A pre-release has its
own note, named with the tail: `1.0.0-beta.2` → `v1_0_0-beta_2.md`.

All three files are validated before any is written, and `docs/api/` is
regenerated afterwards (`openapi.json` carries the version, and CI's
api-docs-freshness gate would go red otherwise). Nothing is committed or
tagged here; read the diff and commit on develop. Merging the release PR into
main tags `v<version>` by itself (release-image.yml's release job) — never tag
by hand (CONTRIBUTING.md → Release prep).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPS = ("ffl_core", "ffl_integrations", "ffl_woo_sync")
# x.y.z with an optional pre-release tail (1.0.0-beta.1); no build metadata.
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?$")
_VERSION_LINE = re.compile(r'^__version__ = "[^"]*"', re.M)


def init_files() -> list[Path]:
	return [ROOT / "apps" / app / app / "__init__.py" for app in APPS]


def note_path(version: str) -> Path:
	"""frappe.utils.change_log reads `change_log/v<major>/v<version with . → _>.md`
	and parses the name back with `_ → .`, so a pre-release keeps its tail:
	1.0.0-beta.1 → v1/v1_0_0-beta_1.md. (It has to: frappe's attempt to strip
	the pre-release before matching, `to_version.prerelease = None`, is a
	no-op in semantic_version 2.10 — the comparison key is cached at parse —
	so a note named for the final release would never match a beta.)"""
	major = SEMVER.match(version).group(1)
	return ROOT / "apps/ffl_core/ffl_core/change_log" / f"v{major}" / f"v{version.replace('.', '_')}.md"


def bump(version: str, *, regenerate_docs: bool = True) -> list[Path]:
	"""Rewrite the three __init__.py; raise SystemExit with a reason if not allowed.
	Validates everything first so a refusal leaves no half-bumped tree."""
	if not SEMVER.match(version):
		raise SystemExit(f"'{version}' is not x.y.z (or x.y.z-pre)")
	note = note_path(version)
	if not note.is_file() or not note.read_text().strip():
		raise SystemExit(f"release note missing or empty: {note.relative_to(ROOT)}\n"
			"Write it first (plain English, for the people at the counter) — see CONTRIBUTING.md.")
	planned: list[tuple[Path, str]] = []
	for path in init_files():
		text = path.read_text()
		if not _VERSION_LINE.search(text):
			raise SystemExit(f"no __version__ line in {path.relative_to(ROOT)}")
		new = _VERSION_LINE.sub(f'__version__ = "{version}"', text, count=1)
		if new != text:
			planned.append((path, new))
	for path, new in planned:
		path.write_text(new)
	if planned and regenerate_docs:
		subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_api_docs.py")], check=True)
	return [p for p, _ in planned]


if __name__ == "__main__":
	if len(sys.argv) != 2:
		raise SystemExit("usage: bump_version.py x.y.z[-pre]")
	for p in bump(sys.argv[1]):
		print(f"bumped {p.relative_to(ROOT)}")
	print(f"version {sys.argv[1]}; next: git add -p, commit on develop, release PR (merging it tags v{sys.argv[1]}; never tag by hand)")
