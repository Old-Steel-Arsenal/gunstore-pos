"""Unit tests for ``ffl_core.compliance.disposition_lock`` — the advisory-lock
serialization and, critically, its DEGRADE path.

Driver (a nightly CI failure): ``_get_lock``
swallowed a transient GET_LOCK failure on the loaded 2-core GH runner and
silently degraded to the unserialized check-insert — two racing bookings then
ran in lockstep and one died on the tabSeries naming collision. The degrade
itself is by design ("never worse than the historical read-then-write"); being
INVISIBLE and UNRETRIED is not. These tests pin the two fixes:

- every degrade logs a warning carrying the reason (exception / NULL / timeout),
  so prod and CI occurrences leave a trace;
- ``book_disposition_once`` retries the lock once before degrading, so a
  one-off transient failure doesn't reopen the double-mint window.

All DB/lock calls are mocked — the real cross-connection race is exercised by
``ffl_integrations.tests.test_fastbound_pos_4473.ConcurrentDispositionBooking``.
"""
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.compliance import disposition_lock
from ffl_core.compliance.disposition_lock import _get_lock, book_disposition_once


class GetLockDegradeObservability(FrappeTestCase):
	"""_get_lock must never degrade silently: False always leaves a warning
	with the reason, True stays quiet."""

	def test_exception_returns_false_and_warns_with_reason(self):
		with mock.patch.object(frappe.db, "sql", side_effect=Exception("boom")), \
				mock.patch.object(frappe, "logger") as logger:
			self.assertFalse(_get_lock("ffldisp:test"))
		logger.return_value.warning.assert_called_once()
		# the trace must carry WHY (exc_info) — a bare "failed" is what made
		# the 2026-07-17 nightly undiagnosable from logs
		_, kwargs = logger.return_value.warning.call_args
		self.assertTrue(kwargs.get("exc_info"),
			"degrade warning must include exc_info so the cause is visible")

	def test_null_result_returns_false_and_warns(self):
		# GET_LOCK returns NULL on server-side errors (e.g. OOM) — MariaDB docs
		with mock.patch.object(frappe.db, "sql", return_value=((None,),)), \
				mock.patch.object(frappe, "logger") as logger:
			self.assertFalse(_get_lock("ffldisp:test"))
		logger.return_value.warning.assert_called_once()

	def test_timeout_zero_returns_false_and_warns(self):
		# GET_LOCK returns 0 when the wait timed out
		with mock.patch.object(frappe.db, "sql", return_value=((0,),)), \
				mock.patch.object(frappe, "logger") as logger:
			self.assertFalse(_get_lock("ffldisp:test"))
		logger.return_value.warning.assert_called_once()

	def test_granted_returns_true_without_warning(self):
		# pin: the happy path must stay quiet (no log spam on every booking)
		with mock.patch.object(frappe.db, "sql", return_value=((1,),)), \
				mock.patch.object(frappe, "logger") as logger:
			self.assertTrue(_get_lock("ffldisp:test"))
		logger.return_value.warning.assert_not_called()


class BookDispositionOnceLockRetry(FrappeTestCase):
	"""One transient GET_LOCK failure must not reopen the unlocked window:
	retry once, and only then degrade."""

	def test_transient_lock_failure_retries_once_then_runs_locked(self):
		with mock.patch.object(disposition_lock, "_get_lock",
					side_effect=[False, True]) as get_lock, \
				mock.patch.object(disposition_lock, "_release_lock") as release, \
				mock.patch.object(frappe.db, "get_value", return_value=None):
			out = book_disposition_once(dedup_key="FB-X|SER-1",
				exists_filters={"fastbound_id": "FB-X", "serial": "SER-1"},
				insert_fn=lambda: "NEW-DISP")
		self.assertEqual(out, "NEW-DISP")
		self.assertEqual(get_lock.call_count, 2,
			"first failure must be retried before degrading")
		release.assert_called_once()

	def test_degrades_unlocked_after_both_attempts_fail_and_never_releases(self):
		with mock.patch.object(disposition_lock, "_get_lock",
					side_effect=[False, False]) as get_lock, \
				mock.patch.object(disposition_lock, "_release_lock") as release, \
				mock.patch.object(frappe.db, "get_value", return_value=None):
			out = book_disposition_once(dedup_key="FB-X|SER-1",
				exists_filters={"fastbound_id": "FB-X", "serial": "SER-1"},
				insert_fn=lambda: "NEW-DISP")
		# the degrade path itself is by design: still books exactly once,
		# never RELEASE_LOCKs a lock it doesn't hold
		self.assertEqual(out, "NEW-DISP")
		self.assertEqual(get_lock.call_count, 2)
		release.assert_not_called()

	def test_existing_row_short_circuits_insert(self):
		# pin: idempotency identity — a pre-existing row wins, insert_fn untouched
		insert_fn = mock.MagicMock()
		with mock.patch.object(disposition_lock, "_get_lock", return_value=True), \
				mock.patch.object(disposition_lock, "_release_lock"), \
				mock.patch.object(frappe.db, "get_value", return_value="EXISTING-DISP"):
			out = book_disposition_once(dedup_key="FB-X|SER-1",
				exists_filters={"fastbound_id": "FB-X", "serial": "SER-1"},
				insert_fn=insert_fn)
		self.assertEqual(out, "EXISTING-DISP")
		insert_fn.assert_not_called()
