"""Sales Invoice ``before_cancel`` guard.

A counter drop-ship SI has NO local FFL Disposition / ShipStation record, so
ffl_core's ``_editable_or_throw`` can't stop a cancel/amend. But once its Distributor
Order is PLACED at RSR the goods are en route and there is no cancel API — so
cancelling (or amending, which cancels first) must be blocked and ops alerted.
Orders not yet placed (Draft/Queued) are withdrawn instead, so a cancelled sale
never leaves a confirmable Draft order behind.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ffl_integrations.distributor import counter, order_state

_dict = counter.frappe._dict


class CancelGuardTest(unittest.TestCase):
	def _run(self, dos, *, doc_name="SINV-1", flags=None):
		"""Returns (withdrawn, alerts). Alerts are captured so the rule that
		a COMPLETE order lets the cancel through but must NEVER be silent is a
		real assertion rather than an absence of one."""
		saved, alerts = [], []

		def _get_doc(dt, name=None, *a, **k):
			d = types.SimpleNamespace(name=name, status="Draft", hold_reason=None)
			d.save = lambda *a, **k: saved.append((d.name, d.status))
			return d

		from ffl_integrations.distributor import cancellation

		ps = [
			mock.patch.object(counter.frappe, "get_all",
				lambda *a, **k: [_dict(**d) for d in dos]),
			# ONE get_doc patch is enough: `counter.frappe` and `cancellation.frappe`
			# are the SAME module object, so patching both targeted one attribute
			# twice — and the second patcher then held a STUB as its "original".
			mock.patch.object(counter.frappe, "get_doc", _get_doc),
			# withdraw re-reads status under for_update; keep it withdrawable.
			mock.patch.object(cancellation.frappe.db, "get_value",
				lambda *a, **k: order_state.DRAFT),
			# cancellation binds notify_admins at import, so patch THAT name — patching
			# ffl_integrations.notifications would leave the bound reference untouched.
			mock.patch.object(cancellation, "notify_admins",
				lambda title, body, **k: alerts.append(title)),
		]
		for p in ps:
			p.start()
			# Register each stop separately: unittest runs cleanups LIFO, which
			# unwinds overlapping patches correctly. Stopping them FIFO in one
			# cleanup restores a STUB as the 'original' when two patchers target
			# the same attribute — that leaked frappe.get_doc process-wide.
			self.addCleanup(p.stop)
		counter.guard_sales_invoice_cancel(
			types.SimpleNamespace(name=doc_name, flags=_dict(flags or {})),
			method="before_cancel")
		self._alerts = alerts
		return saved

	def test_placed_do_blocks_cancel(self):
		with self.assertRaises(counter.frappe.ValidationError):
			self._run([{"name": "DO-1", "status": order_state.PLACED}])

	def test_held_do_blocks_cancel(self):
		with self.assertRaises(counter.frappe.ValidationError):
			self._run([{"name": "DO-1", "status": order_state.HOLD}])

	def test_no_distributor_orders_allows_cancel(self):
		self.assertEqual(self._run([]), [])  # no throw, nothing withdrawn

	def test_draft_do_is_withdrawn_and_cancel_allowed(self):
		saved = self._run([{"name": "DO-1", "status": order_state.DRAFT}])
		self.assertEqual(saved, [("DO-1", order_state.CANCELLED)])

	def test_queued_do_is_withdrawn(self):
		saved = self._run([{"name": "DO-1", "status": order_state.QUEUED}])
		self.assertEqual(saved, [("DO-1", order_state.CANCELLED)])

	def test_error_do_blocks_cancel(self):
		# ERROR may have reached RSR before failing — treat as live, never
		# silently withdraw.
		with self.assertRaises(counter.frappe.ValidationError):
			self._run([{"name": "DO-1", "status": order_state.ERROR}])

	def test_complete_do_allows_cancel_but_alerts(self):
		# Goods already shipped — the cancel is ALLOWED (the sale is being
		# undone) but must raise a human alert; the return is a manual SOP process.
		withdrawn = self._run([{"name": "DO-1", "status": order_state.COMPLETE}])
		self.assertEqual(withdrawn, [])          # nothing to withdraw
		self.assertTrue(self._alerts, "a shipped order must never cancel silently")

	def test_an_edit_carries_a_live_order_instead_of_refusing(self):
		# Editing cancels the invoice only to rebuild it, and hands the live order
		# to the invoice that replaces it — there is no return to coordinate, and
		# no alert to raise.
		withdrawn = self._run([{"name": "DO-1", "status": order_state.PLACED}],
			flags={"ffl_edit_carry_forward": True})
		self.assertEqual(withdrawn, [])
		self.assertEqual(self._alerts, [])

	def test_an_edit_still_withdraws_an_unplaced_order(self):
		# Draft/Queued orders are re-created by the rebuild, so they are withdrawn
		# on an edit exactly as on any other cancel.
		saved = self._run([{"name": "DO-1", "status": order_state.DRAFT}],
			flags={"ffl_edit_carry_forward": True})
		self.assertEqual(saved, [("DO-1", order_state.CANCELLED)])


if __name__ == "__main__":
	unittest.main()
