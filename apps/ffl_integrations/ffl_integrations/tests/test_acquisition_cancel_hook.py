"""FFL Acquisition on_cancel → FastBound DELETE for non-PR rows:
A cancelled bound-book entry must leave FastBound too, or the two books
diverge; PR-linked rows are handled by the Purchase Receipt cancel path."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.fastbound import tasks


class AcquisitionCancelHook(FrappeTestCase):
	def _doc(self, **over):
		base = dict(
			name="ACQ-CANCEL-T", purchase_receipt=None,
			fastbound_id="fb-acq-1", serial="NO-SUCH-SERIAL-CT",
		)
		base.update(over)
		return frappe._dict(base)

	def test_non_pr_acquisition_enqueues_fastbound_delete(self):
		with patch.object(tasks.frappe, "enqueue") as spy:
			tasks.enqueue_delete_on_cancel(self._doc())
		self.assertTrue(spy.called)
		self.assertTrue(spy.call_args.args[0].endswith("delete_acquisition_from_fastbound"))
		self.assertEqual(spy.call_args.kwargs["acquisition_name"], "ACQ-CANCEL-T")
		self.assertEqual(spy.call_args.kwargs["fastbound_id"], "fb-acq-1")
		self.assertTrue(spy.call_args.kwargs["enqueue_after_commit"])

	def test_pr_linked_acquisition_skipped(self):
		"""The Purchase Receipt cancel path owns its rows — a second DELETE
		from this hook would double-fire."""
		with patch.object(tasks.frappe, "enqueue") as spy:
			tasks.enqueue_delete_on_cancel(self._doc(purchase_receipt="MAT-PRE-X"))
		self.assertFalse(spy.called)

	def test_unsynced_acquisition_enqueues_nothing(self):
		with patch.object(tasks.frappe, "enqueue") as spy:
			tasks.enqueue_delete_on_cancel(self._doc(fastbound_id=None))
		self.assertFalse(spy.called)


if __name__ == "__main__":
	unittest.main()
