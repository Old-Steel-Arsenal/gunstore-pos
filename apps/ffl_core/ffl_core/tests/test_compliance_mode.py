"""Paper 4473 Mode: the store-level switch, and what the submit gate then tells staff.

The gate itself is unchanged and deliberately so — a firearm still does not leave
without a recorded 4473 and NICS. What changes is HOW that record is produced, and
therefore what the refusal message should tell the person standing at the counter.
Naming the wrong action is how a cashier ends up waiting for a FastBound webhook
that is never coming.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.compliance.firearm import require_4473_and_nics_for_firearm_sale
from ffl_core.compliance.mode import paper_4473_mode
from ffl_core.tests.settings_sandbox import SettingsSandbox
from ffl_core.utils import single_flag

SETTINGS = "FFL Settings"


class SingleFlagReads(SettingsSandbox, FrappeTestCase):
	"""``single_flag`` is what keeps a shipped default of 1 from reading as 0 on
	every site that has never saved the Single (see its docstring)."""

	FIELD = "paper_4473_mode"

	def _clear(self) -> None:
		self.clear_single(SETTINGS, self.FIELD)

	def test_missing_row_returns_the_declared_default(self):
		self._clear()
		self.assertFalse(single_flag(SETTINGS, self.FIELD, default=0))
		self.assertTrue(single_flag(SETTINGS, self.FIELD, default=1),
			"a field nobody has saved must read as ITS default, not as 0")

	def test_empty_string_counts_as_unset(self):
		self.set_single(SETTINGS, self.FIELD, "")
		self.assertTrue(single_flag(SETTINGS, self.FIELD, default=1))

	def test_explicit_values_win_over_the_default(self):
		self.set_single(SETTINGS, self.FIELD, 0)
		self.assertFalse(single_flag(SETTINGS, self.FIELD, default=1))
		self.set_single(SETTINGS, self.FIELD, 1)
		self.assertTrue(single_flag(SETTINGS, self.FIELD, default=0))


class PaperModeSwitch(SettingsSandbox, FrappeTestCase):
	def test_defaults_off(self):
		"""Every existing site migrates into the electronic flow it already runs."""
		self.clear_single(SETTINGS, "paper_4473_mode")
		self.assertFalse(paper_4473_mode())

	def test_reads_the_setting(self):
		self.set_single(SETTINGS, "paper_4473_mode", 1)
		self.assertTrue(paper_4473_mode())


class GateMessageMatchesTheStoresWorkflow(SettingsSandbox, FrappeTestCase):
	"""The gate blocks identically in both modes; only the instructions differ."""

	def _doc(self, *, form_status="Pending", nics_status="Proceed"):
		return SimpleNamespace(
			items=[SimpleNamespace(idx=1, item_code="ANY-GUN", serial_no="S1")],
			form_4473_status=form_status,
			nics_status=nics_status,
			docstatus=1,
			customer=None,
		)

	def _blocked_message(self, doc) -> str:
		with patch("ffl_core.compliance.firearm.is_regulated_firearm_item", return_value=True):
			with self.assertRaises(frappe.ValidationError) as ctx:
				require_4473_and_nics_for_firearm_sale(doc)
		return str(ctx.exception)

	def test_paper_mode_points_at_the_checkbox_not_fastbound(self):
		self.set_single(SETTINGS, "paper_4473_mode", 1)
		msg = self._blocked_message(self._doc())
		self.assertIn("Record Paper 4473", msg)
		self.assertNotIn("wait for the webhook", msg,
			"a paper-book store has no webhook to wait for")

	def test_electronic_mode_names_the_way_out_of_the_dead_end(self):
		"""A store that switched FastBound off but has not switched Paper 4473 Mode
		on cannot satisfy this gate by ANY counter action. The refusal must name the
		setting to change rather than tell them to wait forever."""
		self.set_single(SETTINGS, "paper_4473_mode", 0)
		msg = self._blocked_message(self._doc())
		self.assertIn("FastBound 4473", msg)
		self.assertIn("Paper 4473 Mode", msg)

	def test_nics_refusal_carries_the_same_instructions(self):
		self.set_single(SETTINGS, "paper_4473_mode", 1)
		msg = self._blocked_message(self._doc(form_status="Completed", nics_status="Delayed"))
		self.assertIn("NICS", msg)
		self.assertIn("Record Paper 4473", msg)

	def test_paper_mode_does_not_weaken_the_gate(self):
		"""The switch changes wording and workflow — never whether an unrecorded
		firearm transfer can be submitted."""
		self.set_single(SETTINGS, "paper_4473_mode", 1)
		for form_status, nics in (("", "Proceed"), ("Pending", "Proceed"),
				("Completed", ""), ("Completed", "Denied"), ("Completed", "Delayed")):
			with self.subTest(form_status=form_status, nics=nics):
				self._blocked_message(self._doc(form_status=form_status, nics_status=nics))

	def test_paper_mode_still_passes_a_recorded_transfer(self):
		self.set_single(SETTINGS, "paper_4473_mode", 1)
		doc = self._doc(form_status="Completed", nics_status="Proceed")
		with patch("ffl_core.compliance.firearm.is_regulated_firearm_item", return_value=True):
			require_4473_and_nics_for_firearm_sale(doc)  # must not raise
