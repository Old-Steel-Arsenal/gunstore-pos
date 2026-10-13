"""Shared serial-splitter — the single home for parsing an ERPNext serial_no
field into individual serials.

A ``POS Invoice Item.serial_no`` (or any serialized line) can carry MULTIPLE
serials joined by newlines when qty>1 — ERPNext core semantics. Treating that
whole string as one serial is the past invoice bug (2× CZ 75 on one
line, serial_no "J4940\\n7805C"). Every 4473 / A&D call site must split through
this one helper so they can't drift.
"""
from __future__ import annotations

from frappe.tests.utils import FrappeTestCase


class SplitSerials(FrappeTestCase):
	def test_single_serial(self):
		from ffl_core.utils import split_serials
		self.assertEqual(split_serials("ABC123"), ["ABC123"])

	def test_two_newline_joined_serials(self):
		from ffl_core.utils import split_serials
		# The incident shape: two guns on one POS Invoice line.
		self.assertEqual(split_serials("J4940\n7805C"), ["J4940", "7805C"])

	def test_strips_whitespace_and_drops_blank_tokens(self):
		from ffl_core.utils import split_serials
		self.assertEqual(split_serials("  A \n\n  B  \n"), ["A", "B"])

	def test_handles_crlf(self):
		from ffl_core.utils import split_serials
		self.assertEqual(split_serials("A\r\nB"), ["A", "B"])

	def test_none_and_empty_yield_empty_list(self):
		from ffl_core.utils import split_serials
		self.assertEqual(split_serials(None), [])
		self.assertEqual(split_serials(""), [])
		self.assertEqual(split_serials("   \n  "), [])

	def test_preserves_order_and_case(self):
		from ffl_core.utils import split_serials
		# Serials are matched case-as-stored everywhere else — no uppercasing.
		self.assertEqual(split_serials("bZ9\naA1"), ["bZ9", "aA1"])
