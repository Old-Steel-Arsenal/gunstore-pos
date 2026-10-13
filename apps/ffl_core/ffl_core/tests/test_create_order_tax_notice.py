"""Create Order says so when it cannot calculate sales tax, read off the shipped JS.

Until quote_config has answered with a home state (or after it failed, e.g. the
Company has no address) the page previews no tax. A blank tax line would read as
"no tax due", so the totals area shows "Sales tax not calculated: <reason>". This
repo ships no JS test runner for pages, so the rule is asserted on the source.
"""
from __future__ import annotations

import unittest

from ffl_core.tests.front_end_invariants import block, code_only, read


def _page() -> str:
	return read("ffl_core", "ffl_core", "page", "create_order", "create_order.js")


class NoHomeStateNoticeTest(unittest.TestCase):
	def test_the_totals_area_shows_the_notice_while_the_home_state_is_unknown(self):
		body = code_only(block(_page(), "\t_updateTotals() {", "\t"))
		self.assertIn("net > 0 && !this._homeState ? this._noHomeStateNotice()", body)

	def test_the_notice_names_the_reason_and_is_not_just_blank(self):
		src = _page()
		notice = block(src, "\t_noHomeStateNotice() {", "\t")
		self.assertIn("__('Sales tax not calculated')}: ", notice)
		self.assertIn("could not be loaded", notice)
		self.assertIn("have not loaded yet", notice)

	def test_a_failed_quote_config_re_renders_the_totals(self):
		load = code_only(block(_page(), "\t_loadTaxConfig() {", "\t"))
		self.assertIn("error: () => { this._taxConfigFailed = true; this._updateTotals(); }", load)
		self.assertIn("this._taxConfigFailed = false;", load)

	def test_no_tax_is_previewed_without_a_home_state(self):
		body = code_only(block(_page(), "\t_inState() {", "\t"))
		self.assertIn("if (this._taxExempt || !this._homeState) return false;", body)


if __name__ == "__main__":
	unittest.main()
