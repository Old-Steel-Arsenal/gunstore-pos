"""Confirming the listings we asked GunBroker to end but could not verify.

END_PENDING is the one listing state that is dangerous in the direction of a
double sale: the gun has usually already left the shelf — it was ended because
it sold at the counter or on the web store — while its GunBroker listing may
still be buyable. Hence the fifteen-minute timer rather than the hourly sweep.

Two of these tests are about NOT concluding things, and they are the point.
Every branch that cannot obtain a clear "inactive, unsold" answer has to leave
the flag exactly where it was, because "we could not confirm" is the state we
are already in — turning a failed question into an answer is what would make
this job a hazard instead of a safety net.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_integrations.gunbroker import end_pending
from ffl_integrations.gunbroker.client import GunBrokerError
from ffl_integrations.tests import gunbroker_fixtures as fx

# Captured before any test patches it: _EndPendingCase stubs _item_sold out for
# every case, and one test needs the real one.
_REAL_ITEM_SOLD = end_pending._item_sold


class _EndPendingCase(FrappeTestCase):
	def setUp(self):
		self.written: dict = {}

		def _set_value(_doctype, name, field, value=None, **_kw):
			updates = field if isinstance(field, dict) else {field: value}
			self.written.setdefault(name, {}).update(updates)

		self._patch("frappe.db.set_value", side_effect=_set_value)
		self._patch_object(end_pending, "_item_sold", return_value=False)

	def _patch(self, target, *args, **kw):
		patcher = patch(target, *args, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _patch_object(self, target, attr, *args, **kw):
		patcher = patch.object(target, attr, *args, **kw)
		started = patcher.start()
		self.addCleanup(patcher.stop)
		return started

	def _row(self):
		return {"name": "SN-001", "gb_item_id": "16087230", "gb_item_history": ""}

	def _client(self, verdict):
		client = MagicMock()
		client.item_probe.return_value = verdict
		return client


class TestConfirmingOne(_EndPendingCase):
	def test_an_inactive_unsold_listing_is_confirmed_ended(self):
		client = self._client({"state": "ok",
			"item": fx.item_detail(is_active=False)})
		self.assertTrue(end_pending.confirm_one(self._row(), client=client))
		written = self.written["SN-001"]
		self.assertEqual(written["gb_end_pending"], 0)
		self.assertEqual(written["gb_item_id"], "")
		self.assertEqual(written["gb_delisted"], 1)

	def test_ending_a_listing_marks_it_ended_and_never_unlisted(self):
		# UNLISTED means "this gun has never been on GunBroker, help yourself",
		# and a machine path will list it again. A gun through this arm HAS been
		# listed and may have been bought.
		client = self._client({"state": "ok",
			"item": fx.item_detail(is_active=False)})
		end_pending.confirm_one(self._row(), client=client)
		self.assertEqual(self.written["SN-001"]["gb_delisted"], 1)

	def test_the_old_item_number_goes_into_history(self):
		client = self._client({"state": "ok",
			"item": fx.item_detail(is_active=False)})
		end_pending.confirm_one(self._row(), client=client)
		self.assertIn("16087230", self.written["SN-001"]["gb_item_history"])

	def test_a_listing_still_active_stays_pending(self):
		client = self._client({"state": "ok",
			"item": fx.item_detail(is_active=True)})
		self.assertFalse(end_pending.confirm_one(self._row(), client=client))
		self.assertEqual(self.written, {})

	def test_a_listing_with_a_buyer_is_never_marked_ended(self):
		# The delete lost a race with a purchase. Marking it ENDED would bury a
		# sale nobody has fulfilled.
		self._patch_object(end_pending, "_item_sold", return_value=True)
		client = self._client({"state": "ok",
			"item": fx.item_detail(is_active=False, has_winning_bid=True)})
		self.assertFalse(end_pending.confirm_one(self._row(), client=client))
		self.assertEqual(self.written, {})

	def test_a_404_is_not_confirmation(self):
		# It is also what a bad item number looks like. The sweep owns that
		# judgement — it has the two-round rule and the SKU scans this job
		# deliberately does not do.
		client = self._client({"state": "gone", "item": None})
		self.assertFalse(end_pending.confirm_one(self._row(), client=client))
		self.assertEqual(self.written, {})

	def test_only_an_ok_state_can_confirm(self):
		# The branch above passes for the wrong reason on its own: a real `gone`
		# verdict carries no item body, so the inactive test would refuse it even
		# with the state check gone. This pairs a non-ok state with a body that
		# WOULD confirm — a shape the client never produces — so that the state
		# check is what is being asserted rather than a side effect of it.
		client = self._client({"state": "gone",
			"item": fx.item_detail(is_active=False)})
		self.assertFalse(end_pending.confirm_one(self._row(), client=client))
		self.assertEqual(self.written, {})

	def test_an_unreachable_gunbroker_is_not_confirmation(self):
		client = self._client({"state": "error", "item": None})
		self.assertFalse(end_pending.confirm_one(self._row(), client=client))
		self.assertEqual(self.written, {})

	def test_a_raised_error_leaves_the_flag_alone(self):
		client = MagicMock()
		client.item_probe.side_effect = GunBrokerError("timeout")
		with patch.object(end_pending, "safe_log_error", side_effect=lambda *a: None):
			self.assertFalse(end_pending.confirm_one(self._row(), client=client))
		self.assertEqual(self.written, {})


class TestSelection(_EndPendingCase):
	def test_only_flagged_guns_with_an_item_number_are_asked_about(self):
		# A flagged gun with no item number is one the manual strategy raised
		# for a human: there is nothing to ask GunBroker, and asking would be a
		# request for item "".
		with patch("frappe.db.sql", return_value=[]) as sql:
			end_pending.end_pending_serials()
		query = sql.call_args[0][0]
		self.assertIn("gb_end_pending", query)
		self.assertIn("gb_item_id", query)
		self.assertIn("!= ''", query)


class TestTheGate(_EndPendingCase):
	def setUp(self):
		super().setUp()
		self._patch_object(end_pending, "_sold_predicate",
			return_value=lambda item: False)

	def test_nothing_is_asked_while_the_integration_is_off(self):
		self._patch_object(end_pending, "gunbroker_enabled", return_value=False)
		enqueue = self._patch("frappe.enqueue")
		serials = self._patch_object(end_pending, "end_pending_serials")
		end_pending.poll_end_pending()
		enqueue.assert_not_called()
		serials.assert_not_called()

	def test_the_tick_only_enqueues_and_does_no_outbound_work(self):
		# A round is up to BATCH separate GET /Items/{id} calls; a Cron
		# scheduled job gets the default queue's 300 seconds and no resume
		# point when it is cut off. The dedupe is what stops a slow round
		# having a second one queued behind it.
		self._patch_object(end_pending, "gunbroker_enabled", return_value=True)
		self._patch("ffl_core.utils.single_flag", return_value=True)
		enqueue = self._patch("frappe.enqueue")
		serials = self._patch_object(end_pending, "end_pending_serials")
		summary = end_pending.poll_end_pending()
		self.assertTrue(summary["enqueued"])
		serials.assert_not_called()
		args, kwargs = enqueue.call_args
		self.assertEqual(
			args[0], "ffl_integrations.gunbroker.end_pending._run_end_pending")
		self.assertEqual(kwargs["queue"], "long")
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["job_id"], end_pending.END_PENDING_JOB_ID)

	def test_the_cron_entry_never_raises(self):
		self._patch_object(end_pending, "gunbroker_enabled",
			side_effect=RuntimeError("settings table is missing"))
		self._patch_object(end_pending, "safe_log_error", side_effect=lambda *a: None)
		end_pending.poll_end_pending()  # must not raise


class TestTheRound(_EndPendingCase):
	def setUp(self):
		super().setUp()
		self._patch_object(end_pending, "_sold_predicate",
			return_value=lambda item: False)

	def test_an_empty_queue_builds_no_client(self):
		# A store with nothing pending must make no GunBroker call at all — not
		# even the login one a client construction would trigger.
		self._patch_object(end_pending, "end_pending_serials", return_value=[])
		client = self._patch_object(end_pending, "GunBrokerClient")
		summary = end_pending._run_end_pending()
		client.from_settings.assert_not_called()
		self.assertEqual(summary["considered"], 0)

	def test_the_round_counts_what_it_settled(self):
		self._patch_object(end_pending, "end_pending_serials",
			return_value=[self._row(), {"name": "SN-002", "gb_item_id": "16087231",
				"gb_item_history": ""}])
		self._patch_object(end_pending, "GunBrokerClient")
		self._patch_object(end_pending, "safe_commit", side_effect=lambda: None)
		self._patch_object(end_pending, "confirm_one",
			side_effect=[True, False])
		summary = end_pending._run_end_pending()
		self.assertEqual(summary, {"considered": 2, "confirmed": 1,
			"still_pending": 1, "dependency_missing": False})


class TestTheMissingDependency(_EndPendingCase):
	"""The predicate this job shares with the sweep ships in a later PR.

	Until that one is merged the import raises inside the per-serial loop,
	where the blanket handler eats it: an Error Log every fifteen minutes and
	a summary reading zero confirmed — which is also exactly what a healthy
	quiet account looks like. A safety net is allowed to find nothing; it is
	not allowed to be switched off and say nothing.
	"""

	def setUp(self):
		super().setUp()
		self.alerts: list[str] = []
		self._patch_object(end_pending, "notify_admins",
			side_effect=lambda s, m, **kw: self.alerts.append(kw["dedup_key"]))

	def _hide_the_module(self):
		real = __import__

		def _fake(name, *a, **kw):
			if name == "ffl_integrations.gunbroker.doc_events":
				raise ImportError("No module named 'ffl_integrations.gunbroker.doc_events'")
			return real(name, *a, **kw)

		return patch("builtins.__import__", side_effect=_fake)

	def test_the_gate_refuses_to_enqueue_a_job_that_cannot_work(self):
		self._patch_object(end_pending, "gunbroker_enabled", return_value=True)
		self._patch("ffl_core.utils.single_flag", return_value=True)
		enqueue = self._patch("frappe.enqueue")
		with self._hide_the_module():
			summary = end_pending.poll_end_pending()
		self.assertTrue(summary["dependency_missing"])
		self.assertFalse(summary["enqueued"])
		enqueue.assert_not_called()
		self.assertEqual(self.alerts, ["gb_end_pending_dependency_missing"])

	def test_the_round_says_so_rather_than_reporting_nothing_to_do(self):
		serials = self._patch_object(end_pending, "end_pending_serials")
		with self._hide_the_module():
			summary = end_pending._run_end_pending()
		self.assertTrue(summary["dependency_missing"])
		self.assertEqual(summary["considered"], 0)
		serials.assert_not_called()

	def test_a_direct_caller_gets_the_answer_that_changes_nothing(self):
		# "There may be a buyer" leaves the listing exactly where it was.
		with self._hide_the_module():
			self.assertTrue(_REAL_ITEM_SOLD({}))
