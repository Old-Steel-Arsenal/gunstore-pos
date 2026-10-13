#!/usr/bin/env python3
"""Start a new POS extension from the skeleton — `make ext-new`.

    python3 scripts/ext_new.py --id acme_hello --title "Acme Hello" --publisher "Acme Software LLC" [--out ../acme_hello]

Copies deploy/prod/tests/ext_template/ into --out (default ../<id>), filling the
placeholders in file names and contents:

    {{id}} acme_hello · {{title}} Acme Hello · {{publisher}} Acme Software LLC
    {{vendor}} acme · {{slug}} acme-hello · {{module}} acme_hello (the module package
    frappe imports for the title) · {{year}} / {{date}} from --date (default today)

The result is the standard extension shape — manifest and hooks, an @extension_hook
handler, a gated and documented endpoint, a POS registry bundle, a release note and
the tests the extension guide asks for (switched off / no role / repeat call) — plus its first
docs/api (scripts/generate_api_docs.py --app), so all three `make ext-test` steps pass
from the start; `python -m ffl_core.extensions.lint` finds 0 errors.
deploy/prod/tests/ext_hello is this script's output for ext_hello (scripts/test_ext_new.py
pins that). stdlib only, so it runs on any host.
"""
from __future__ import annotations

import argparse
import datetime
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE.parent / "deploy" / "prod" / "tests" / "ext_template"
ID_RE = re.compile(r"^[a-z][a-z0-9]{1,11}_[a-z][a-z0-9_]*$")          # ffl_core.extensions.lint.ID_RE
TITLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 -]{0,29}$")               # lint: at most 30 characters; lands in module names
PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


class Refused(Exception):
	pass


def values(app_id: str, title: str, publisher: str, date: datetime.date) -> dict[str, str]:
	if not ID_RE.fullmatch(app_id):
		raise Refused(f"--id {app_id!r} must be <vendor>_<name>: lowercase letters, digits and _, vendor 2–12 characters")
	if not TITLE_RE.fullmatch(title) or title != title.strip():
		raise Refused(f"--title {title!r} must be 1–30 characters of letters, digits, spaces and hyphens, starting with a letter")
	if not publisher.strip() or re.search(r'["\\\x00-\x1f\x7f-\x9f]', publisher):   # lands in Python and TOML strings
		raise Refused(f"--publisher {publisher!r} must be a name without quotes, backslashes or control characters")
	return {
		"id": app_id,
		"title": title,
		"publisher": publisher.strip(),
		"vendor": app_id.split("_", 1)[0],
		"slug": app_id.replace("_", "-"),
		"module": re.sub(r"[^a-z0-9_]", "_", title.lower().replace(" ", "_")),   # frappe.scrub(title), as the lint checks it
		"year": str(date.year),
		"date": f"{date:%B} {date.day}, {date.year}",
	}


def generate(app_id: str, title: str, publisher: str, out: Path | str, date: datetime.date) -> list[Path]:
	"""Write the skeleton for ``app_id`` into ``out`` (which must not exist or be empty)."""
	fill = values(app_id, title, publisher, date)
	out = Path(out)
	existed = out.exists()
	if existed and (not out.is_dir() or any(out.iterdir())):
		raise Refused(f"{out} already exists and is not empty — nothing written")
	written = []
	try:
		for src in sorted(p for p in TEMPLATE.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
			dest = out / PLACEHOLDER.sub(lambda m: fill[m.group(1)], src.relative_to(TEMPLATE).as_posix())
			dest.parent.mkdir(parents=True, exist_ok=True)
			dest.write_text(PLACEHOLDER.sub(lambda m: fill[m.group(1)], src.read_text(encoding="utf-8")), encoding="utf-8")
			written.append(dest)
		subprocess.run([sys.executable, str(HERE / "generate_api_docs.py"), "--app", str(out)], check=True, stdout=subprocess.DEVNULL)
	except BaseException:
		shutil.rmtree(out, ignore_errors=True)          # no half-made skeleton: the folder was empty or missing
		if existed:
			out.mkdir()
		raise
	return written + sorted((out / "docs" / "api").rglob("*.*"))


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--id", required=True, help="<vendor>_<name>, e.g. acme_layaway")
	parser.add_argument("--title", required=True, help='what staff read, e.g. "Acme Layaway" (at most 30 characters)')
	parser.add_argument("--publisher", required=True, help='who publishes it, e.g. "Acme Software LLC"')
	parser.add_argument("--out", help="where to create it (default: ../<id>)")
	parser.add_argument("--date", help="release date of 0.1.0, YYYY-MM-DD (default: today)")
	opts = parser.parse_args(argv)
	try:
		date = datetime.date.fromisoformat(opts.date) if opts.date else datetime.date.today()
	except ValueError:
		print(f"ext-new: --date {opts.date!r} must be YYYY-MM-DD", file=sys.stderr)
		return 2
	out = Path(opts.out) if opts.out else Path("..") / opts.id
	try:
		written = generate(opts.id, opts.title, opts.publisher, out, date)
	except Refused as exc:
		print(f"ext-new: {exc}", file=sys.stderr)
		return 2
	except (OSError, subprocess.CalledProcessError) as exc:
		print(f"ext-new: {exc} — nothing written", file=sys.stderr)
		return 1
	print(f"created {opts.id} in {out} ({len(written)} files). Next, from this checkout with the dev stack up:")
	print(f"  make ext-test ID={opts.id} DIR={out}      # install on the dev site, run its tests, lint, API-docs check")
	print(f"  python3 scripts/generate_api_docs.py --app {out}      # after changing an endpoint: regenerate its docs/api/")
	return 0


if __name__ == "__main__":
	sys.exit(main())
