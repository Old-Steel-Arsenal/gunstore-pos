"""ffl_core.utils.guest_job_elevated — the Guest-job elevation guard.

A background job enqueued from an ``allow_guest`` webhook runs as Guest
(frappe.enqueue captures frappe.session.user); ERPNext's own permission checks
then refuse it. The guard elevates ONLY that case and always restores.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.utils import guest_job_elevated


class TestGuestJobElevated(FrappeTestCase):

	def setUp(self):
		self._prev = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._prev)

	def test_guest_runs_as_administrator_and_is_restored(self):
		frappe.set_user("Guest")
		with guest_job_elevated():
			self.assertEqual(frappe.session.user, "Administrator")
		self.assertEqual(frappe.session.user, "Guest")

	def test_restores_guest_even_when_the_block_raises(self):
		frappe.set_user("Guest")
		with self.assertRaises(RuntimeError):
			with guest_job_elevated():
				raise RuntimeError("boom")
		self.assertEqual(frappe.session.user, "Guest")

	def test_a_real_user_keeps_their_own_identity(self):
		frappe.set_user("Administrator")
		with guest_job_elevated():
			self.assertEqual(frappe.session.user, "Administrator")
		self.assertEqual(frappe.session.user, "Administrator")
