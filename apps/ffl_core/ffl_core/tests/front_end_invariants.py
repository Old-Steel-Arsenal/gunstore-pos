"""Source-level helpers for the front-end invariant tests (test_pending_4473_orders,
test_pending_dispositions_front_end_invariants and the extensions' own copies).
This repo ships no JS test runner, so these read the shipped JS and assert on
its text."""
from __future__ import annotations

import os
import re

import frappe


def read(app: str, *path: str) -> str:
	with open(os.path.join(frappe.get_app_path(app, *path))) as f:
		return f.read()


def block(source: str, opener: str, indent: str) -> str:
	"""The text from `opener` up to the next sibling declaration at `indent`.

	Good enough for these files, where methods/functions sit at exactly one
	level of indentation and everything they contain sits deeper — and far less
	brittle than counting braces through template literals.
	"""
	start = source.index(opener)
	rest = source[start + len(opener):]
	nxt = re.search(rf"\n{indent}(?:function )?[\w_]+\s*\(", rest)
	return rest[: nxt.start()] if nxt else rest


def code_only(source: str) -> str:
	"""Drop whole-line // comments, so an assertion that measures the ORDER of
	two calls does not measure the comment that names both, in the wrong
	sequence, for emphasis. (A `//` mid-line is left alone: it could be a URL.)"""
	return "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("//"))


