"""Unit tests for check_comment_only.py (run in release-image.yml's api-docs-freshness job)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_comment_only as cc  # noqa: E402


class PyCheck(unittest.TestCase):
	def test_comment_and_docstring_edits_pass(self):
		a = 'def f(x):\n\t"""old"""\n\treturn x  # old\n'
		b = 'def f(x):\n\t"""new, longer\n\n\twords"""\n\t# added\n\treturn x\n'
		self.assertIsNone(cc.compare("a.py", a, b))

	def test_removing_a_docstring_passes_but_a_code_change_fails(self):
		self.assertIsNone(cc.compare("a.py", 'def f():\n\t"""d"""\n', "def f():\n\tpass\n"))
		self.assertTrue(cc.compare("a.py", "x = 1\n", "x = 2\n"))

	def test_a_string_statement_that_is_not_a_docstring_counts(self):
		a = 'def f():\n\tx = 1\n\t"note"\n'
		b = 'def f():\n\tx = 1\n\t"other note"\n'
		self.assertTrue(cc.compare("a.py", a, b))

	def test_message_strings_count(self):
		self.assertTrue(cc.compare("a.py", 'raise ValueError("a")\n', 'raise ValueError("b")\n'))


class JsCheck(unittest.TestCase):
	def test_comments_only(self):
		a = "// head\nvar a = 1; /* old */\nvar r = x / 2; // tail\n"
		b = "var a = 1;\n// new\nvar r = x / 2;\n"
		self.assertIsNone(cc.compare("a.js", a, b))

	def test_comment_markers_inside_strings_and_regex_are_code(self):
		self.assertTrue(cc.compare("a.js", 'var u = "http://a";\n', 'var u = "http:";\n'))
		self.assertTrue(cc.compare("a.js", "var r = /a\\/\\/b/;\n", "var r = /a/;\n"))

	def test_code_change_fails(self):
		self.assertTrue(cc.compare("a.js", "var a = 1; // x\n", "var a = 2; // x\n"))

	def test_template_literal_text_counts(self):
		self.assertTrue(cc.compare("a.js", "var t = `a ${b} // c`;\n", "var t = `a ${b} c`;\n"))


if __name__ == "__main__":
	unittest.main()
