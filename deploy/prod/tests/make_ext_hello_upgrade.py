#!/usr/bin/env python3
"""Build the ext_hello 0.2.0 upgrade package — the fixture for in-place upgrades.

    python3 deploy/prod/tests/make_ext_hello_upgrade.py <out.zip>

Starts from the 0.1.0 fixture next to this file (deploy/prod/tests/ext_hello, which
stays 0.1.0) and adds one of everything an upgrade has to carry:

  * version 0.2.0 (__version__ and the manifest) and change_log/v0/v0_2_0.md;
  * an idempotent patch, ext_hello.patches.v0_2.remember_greeting (post_model_sync),
    which imports the bundled dependency and stores its greeting — after a migrate,
    `bench --site S execute frappe.db.get_default --args '["ext_hello_greeting"]'`
    prints "Hello from ext_hello_dep 1.0.0", proof the wheel imports where migrate runs;
  * wheels/ext_hello_dep-1.0.0-py3-none-any.whl, a pure-Python wheel built here with
    zipfile (no .whl is committed), declared in pyproject [project].dependencies.

The zip root is the app root (git-archive shape) and passes ffl_core.extensions.lint
with 0 errors (test_extension_lint pins that). stdlib only, so it runs on any host;
the lint tests and test_ext_replay.sh use build_wheel from here.
"""
from __future__ import annotations

import base64
import hashlib
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "ext_hello"
EPOCH = (2026, 1, 1, 0, 0, 0)             # fixed zip timestamps: the same build is the same bytes (and sha256)
VERSION = "0.2.0"
DEP, DEP_VERSION = "ext_hello_dep", "1.0.0"

PATCH = '''import frappe


def execute():
	"""Store the bundled dependency's greeting. Idempotent: a re-run writes the same value."""
	from ext_hello_dep import GREETING

	frappe.db.set_default("ext_hello_greeting", GREETING)
'''


def build_wheel(dest: Path | str, name: str, version: str, files: dict[str, str], *,
		requires: tuple[str, ...] = (), tag: str = "py3-none-any") -> Path:
	"""Write <name>-<version>-<tag>.whl into ``dest``: ``files`` (archive path → text)
	plus a dist-info with METADATA (one Requires-Dist per ``requires``), WHEEL and RECORD."""
	dist_info = f"{name}-{version}.dist-info"
	members = dict(files)
	members[f"{dist_info}/METADATA"] = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n" + "".join(
		f"Requires-Dist: {r}\n" for r in requires)
	members[f"{dist_info}/WHEEL"] = f"Wheel-Version: 1.0\nGenerator: gunstore-ext-tests\nRoot-Is-Purelib: true\nTag: {tag}\n"
	record = []
	for path, text in members.items():
		digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()
		record.append(f"{path},sha256={digest},{len(text.encode())}")
	members[f"{dist_info}/RECORD"] = "\n".join([*record, f"{dist_info}/RECORD,,"]) + "\n"
	whl = Path(dest) / f"{name}-{version}-{tag}.whl"
	with zipfile.ZipFile(whl, "w", zipfile.ZIP_DEFLATED) as z:
		for path, text in members.items():
			_add(z, path, text.encode())
	return whl


def _add(z: zipfile.ZipFile, name: str, data: bytes) -> None:
	info = zipfile.ZipInfo(name, date_time=EPOCH)
	info.external_attr = 0o644 << 16
	info.compress_type = zipfile.ZIP_DEFLATED
	z.writestr(info, data)


def _replace(path: Path, old: str, new: str) -> None:
	text = path.read_text()
	if old not in text:
		raise ValueError(f"{path}: {old!r} not found — did the 0.1.0 fixture change?")
	path.write_text(text.replace(old, new))


def build_upgrade(out: Path | str) -> Path:
	"""The ext_hello 0.2.0 package, zipped at ``out``."""
	out = Path(out).resolve()
	with tempfile.TemporaryDirectory(prefix="ext-hello-upgrade-") as tmp:
		root = Path(tmp) / "ext_hello"
		shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
		pkg = root / "ext_hello"
		_replace(pkg / "__init__.py", '__version__ = "0.1.0"', f'__version__ = "{VERSION}"')
		_replace(pkg / "hooks.py", '"version": "0.1.0"', f'"version": "{VERSION}"')
		_replace(root / "pyproject.toml", "dependencies = []", f'dependencies = ["{DEP}>=1.0,<2"]')
		(pkg / "change_log" / "v0" / "v0_2_0.md").write_text(
			f"### Version {VERSION}\n\n**New**\n- The welcome text comes from a bundled library ({DEP} {DEP_VERSION}).\n")
		(pkg / "patches.txt").write_text("[pre_model_sync]\n\n[post_model_sync]\next_hello.patches.v0_2.remember_greeting\n")
		(pkg / "patches" / "v0_2").mkdir(parents=True)
		(pkg / "patches" / "__init__.py").write_text("")
		(pkg / "patches" / "v0_2" / "__init__.py").write_text("")
		(pkg / "patches" / "v0_2" / "remember_greeting.py").write_text(PATCH)
		(root / "wheels").mkdir()
		build_wheel(root / "wheels", DEP, DEP_VERSION, {
			f"{DEP}/__init__.py": f'__version__ = "{DEP_VERSION}"\nGREETING = "Hello from {DEP} {DEP_VERSION}"\n'})
		out.parent.mkdir(parents=True, exist_ok=True)
		with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
			for p in sorted(root.rglob("*")):
				if p.is_file():
					_add(z, p.relative_to(root).as_posix(), p.read_bytes())
	return out


def main(argv: list[str]) -> int:
	if len(argv) != 1:
		print("usage: make_ext_hello_upgrade.py <out.zip>", file=sys.stderr)
		return 2
	print(build_upgrade(argv[0]))
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
