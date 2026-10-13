"""Individual customer name sync (ffl_core.customer_name): compose the display
name from the parts, or split it into them — on every creation path."""
from __future__ import annotations

import os
import re

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.customer_name import NAME_SUFFIXES, compose_display_name, split_display_name
from ffl_core.setup.customer_identity import _SUFFIX_OPTIONS

_JS_DIR = os.path.join(os.path.dirname(__file__), "..", "public", "js")


def _uniq(prefix: str) -> str:
	"""_Test-prefixed and unique per run: docname == customer_name here, so a
	fixed name would collide with anything a manual E2E left on the site."""
	return f"_Test {prefix} {frappe.generate_hash(length=6)}"


def _customer(**fields):
	doc = frappe.get_doc({"doctype": "Customer", "customer_group": "Individual",
		"territory": "All Territories", **fields})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc


class CustomerNameSync(FrappeTestCase):
	def test_split_and_compose_are_inverse_enough(self):
		self.assertEqual(split_display_name("pat lee IV Sampleton"),
			{"first": "pat", "middle": "lee", "last": "Sampleton", "suffix": "IV"})
		self.assertEqual(split_display_name("Cher"), {"first": "Cher", "middle": "", "last": "", "suffix": ""})
		self.assertEqual(split_display_name("Jr"), {"first": "Jr", "middle": "", "last": "", "suffix": ""})
		self.assertEqual(compose_display_name("Alex", "", "Sample", "Jr"), "Alex Sample Jr")

	def test_parts_only_compose_the_name_and_docname(self):
		last = _uniq("Sample")
		doc = _customer(customer_type="Individual", ffl_first_name="Alex", middle_name="Morgan",
			ffl_last_name=last, name_suffix="Jr")
		self.assertEqual(doc.customer_name, f"Alex Morgan {last} Jr")
		self.assertEqual(doc.name, doc.customer_name)

	def test_correcting_the_display_name_resplits_the_parts(self):
		doc = _customer(customer_type="Individual", customer_name=_uniq("Smith Jon"))
		self.assertEqual(doc.ffl_last_name.split()[-1], doc.customer_name.split()[-1])
		doc.customer_name = _uniq("Brown Jonathan Q")
		doc.save()
		expect = split_display_name(doc.customer_name)
		self.assertEqual((doc.ffl_first_name, doc.middle_name, doc.ffl_last_name, doc.name_suffix),
			(expect["first"], expect["middle"], expect["last"], expect["suffix"]))
		# unchanged display name → typed parts are left alone
		doc.middle_name = "Quincy"
		doc.save()
		self.assertEqual(doc.middle_name, "Quincy")

	def test_suffix_spellings_are_pinned_to_the_select_options(self):
		options = {o for o in _SUFFIX_OPTIONS.split("\n") if o}
		self.assertEqual(set(NAME_SUFFIXES.values()), options)
		for js in ("aamva.js", "customer_quick_entry.js"):
			with open(os.path.join(_JS_DIR, js)) as f:
				src = f.read()
			m = re.search(r"var SUFFIXES = \{([^}]*)\}", src)
			self.assertTrue(m, js)
			js_map = dict(re.findall(r"(\w+):\s*'([^']+)'", m.group(1)))
			self.assertEqual(set(js_map.values()), options, js)
			self.assertEqual({k.lower() for k in js_map}, set(NAME_SUFFIXES), js)

	def test_name_only_is_split_into_parts(self):
		# Same split the Trade-in prefill uses: a generational suffix is pulled
		# out even mid-name, the display name itself is never rewritten.
		name = _uniq("Web Buyer Sr Only")     # "_Test Web Buyer Sr Only <hash>"
		doc = _customer(customer_type="Individual", customer_name=name)
		self.assertEqual((doc.ffl_first_name, doc.middle_name, doc.ffl_last_name, doc.name_suffix),
			("_Test", "Web Buyer Only", name.split()[-1], "Sr"))
		self.assertEqual(doc.customer_name, name)

	def test_typed_values_are_never_overwritten(self):
		display = _uniq("Display Name")
		doc = _customer(customer_type="Individual", customer_name=display,
			ffl_first_name="Real", ffl_last_name="Person")
		self.assertEqual(doc.customer_name, display)
		self.assertEqual((doc.ffl_first_name, doc.ffl_last_name), ("Real", "Person"))
		doc.customer_name = ""            # a later save with a blanked display name is re-composed
		doc.save()
		self.assertEqual(doc.customer_name, "Real Person")

	def test_company_is_untouched(self):
		doc = _customer(customer_type="Company", customer_name=_uniq("Acme Sr Corp"))
		self.assertFalse(doc.get("ffl_first_name"))
		self.assertFalse(doc.get("ffl_last_name"))
