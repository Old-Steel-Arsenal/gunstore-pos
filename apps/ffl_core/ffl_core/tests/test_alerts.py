"""ffl_core.alerts.alert_once — the deduped Error Log + manager ToDo."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core import alerts


class TestAlertOnce(FrappeTestCase):

	def _run(self, *, open_todo, assignees):
		with patch("frappe.db.exists", return_value=open_todo) as exists, \
			 patch("frappe.log_error") as log, \
			 patch.object(alerts, "alert_assignees", return_value=assignees), \
			 patch("frappe.desk.form.assign_to.add") as add:
			out = alerts.alert_once("Sales Invoice", "SI-1", title="t", message="m",
				description="d")
		return out, exists, log, add

	def test_dedupe_gates_the_log_too(self):
		# The whole point: a persistent condition is logged once, not per run.
		out, exists, log, add = self._run(open_todo=True, assignees=["mgr@x"])
		self.assertFalse(out)
		log.assert_not_called()
		add.assert_not_called()
		self.assertEqual(exists.call_args.args[1]["reference_name"], "SI-1")

	def test_new_alert_logs_then_assigns(self):
		out, _e, log, add = self._run(open_todo=False, assignees=["mgr@x"])
		self.assertTrue(out)
		log.assert_called_once_with(title="t", message="m")
		payload = add.call_args.args[0]
		self.assertEqual(payload["doctype"], "Sales Invoice")
		self.assertEqual(payload["name"], "SI-1")
		self.assertEqual(payload["assign_to"], ["mgr@x"])
		self.assertEqual(payload["priority"], "High")

	def test_nobody_to_assign_still_logs(self):
		out, _e, log, add = self._run(open_todo=False, assignees=[])
		self.assertTrue(out)
		log.assert_called_once()
		add.assert_not_called()

	def test_explicit_empty_assignees_means_log_only(self):
		with patch("frappe.db.exists", return_value=False), \
			 patch("frappe.log_error"), \
			 patch.object(alerts, "alert_assignees") as default_list, \
			 patch("frappe.desk.form.assign_to.add") as add:
			alerts.alert_once("X", "1", title="t", message="m", description="d", assignees=[])
		default_list.assert_not_called()
		add.assert_not_called()


class TestDedupeOnTitle(FrappeTestCase):
	"""dedupe_on_title: an unrelated open ToDo must not swallow the alert; the
	same title (since ``since``) must."""

	def test_open_todo_does_not_swallow_a_title_deduped_alert(self):
		def exists(doctype, filters=None, *a, **k):
			return doctype == "ToDo"  # an unrelated open ToDo on the document
		with patch("frappe.db.exists", side_effect=exists) as ex, \
			 patch("frappe.log_error") as log, \
			 patch.object(alerts, "alert_assignees", return_value=[]):
			out = alerts.alert_once("Woo Online Order", "WOO-1", title="t-7001", message="m",
				description="d", dedupe_on_title=True, since="2026-09-27 00:00:00")
		self.assertTrue(out)
		log.assert_called_once_with(title="t-7001", message="m")
		self.assertEqual(ex.call_args.args,
			("Error Log", {"method": "t-7001", "creation": (">=", "2026-09-27 00:00:00")}))

	def test_same_title_is_logged_once(self):
		with patch("frappe.db.exists", side_effect=lambda dt, f=None, *a, **k: dt == "Error Log"), \
			 patch("frappe.log_error") as log:
			out = alerts.alert_once("Woo Online Order", "WOO-1", title="t", message="m",
				description="d", dedupe_on_title=True)
		self.assertFalse(out)
		log.assert_not_called()


class TestAssignFailureIsContained(FrappeTestCase):

	def test_a_failing_assignment_still_leaves_the_error_log(self):
		with patch("frappe.db.exists", return_value=False), \
			 patch("frappe.log_error") as log, \
			 patch.object(alerts, "alert_assignees", return_value=["mgr@x"]), \
			 patch("frappe.desk.form.assign_to.add", side_effect=RuntimeError("smtp")), \
			 patch("frappe.get_traceback", return_value="tb"):
			out = alerts.alert_once("Sales Invoice", "SI-1", title="t", message="m",
				description="d")
		self.assertTrue(out)
		self.assertEqual(log.call_count, 2)   # the alert itself, then the assign failure


class TestAlertAssignees(FrappeTestCase):

	def test_excludes_administrator_and_disabled_users(self):
		with patch("frappe.get_all", return_value=["Administrator", "mgr@x", "off@x"]), \
			 patch("frappe.db.get_value",
				side_effect=lambda dt, name, field: 0 if name == "off@x" else 1):
			self.assertEqual(alerts.alert_assignees(), ["mgr@x"])

	def test_guest_is_excluded_too(self):
		with patch("frappe.get_all", return_value=["Guest", "mgr@x"]), \
			 patch("frappe.db.get_value", return_value=1):
			self.assertEqual(alerts.alert_assignees(), ["mgr@x"])


class TestHardenedLog(FrappeTestCase):
	"""alert_once runs in failure paths; its own logger must not raise."""

	def test_a_raising_log_error_falls_back_to_the_file_logger(self):
		logger = MagicMock()
		with patch("frappe.db.exists", return_value=False), \
			 patch("frappe.log_error", side_effect=RuntimeError("currently_saving")), \
			 patch("frappe.logger", return_value=logger), \
			 patch.object(alerts, "alert_assignees", return_value=[]):
			out = alerts.alert_once("Sales Invoice", "SI-1", title="t", message="m",
				description="d")
		self.assertTrue(out)
		logger.error.assert_called_once()
