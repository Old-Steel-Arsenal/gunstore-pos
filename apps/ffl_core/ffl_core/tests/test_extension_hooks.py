"""Contract tests for the core hooks extensions plug into (docs/extensions/hooks.md).

Each test registers a fake provider (a function in this module) under the hook
and checks the core consumer honours it — and what core assumes when the
provider raises. The real providers (Consignment Out) are exercised by their
own tests; this file pins the contract.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

HERE = "ffl_core.tests.test_extension_hooks"


@contextmanager
def hooks(name: str, values: list):
	"""frappe.get_hooks(name) → values; every other key untouched."""
	real = frappe.get_hooks

	def fake(hook=None, default="_KEEP_", app_name=None):
		if hook == name:
			return list(values)
		return real(hook, app_name=app_name) if default == "_KEEP_" else real(hook, default, app_name)

	with patch.object(frappe, "get_hooks", side_effect=fake):
		yield


def _error_logged(title_fragment: str) -> bool:
	return bool(frappe.db.exists("Error Log", {"method": ["like", f"%{title_fragment}%"]}))


# --- fake providers -----------------------------------------------------------
def holds_a(item_code):
	return [{"serial": "HOLD-A", "source": "fake", "ref": "A-1", "kind": "fake",
		"tooltip": "held by A", "url": "/app/fake/A-1"}]


def holds_b(item_code):
	return [{"serial": "HOLD-A", "source": "fake-b", "ref": "B-1", "kind": "fake", "tooltip": "held by B", "url": ""},
		{"serial": "HOLD-B", "source": "fake-b", "ref": "B-2", "kind": "fake", "tooltip": "held by B", "url": ""}]


def holds_boom(item_code):
	raise RuntimeError("provider down")


def stock_handled_yes(doc):
	return True


def stock_handled_no(doc):
	return False


def stock_handled_boom(doc):
	raise RuntimeError("provider down")


def home_portal(user):
	return "fake-portal"


def home_none(user):
	return None


def home_boom(user):
	raise RuntimeError("provider down")


TOGGLES: list[tuple[str, bool]] = []


def on_toggled(app, enabled):
	TOGGLES.append((app, enabled))


def toggled_boom(app, enabled):
	raise RuntimeError("provider down")


# --- gunshop_counter_holds ------------------------------------------------------
class TestCounterHolds(FrappeTestCase):
	ITEM = "ZZ-NO-SUCH-ITEM-FOR-HOOK-TEST"

	def test_provider_rows_are_merged_first_provider_wins(self):
		from ffl_core.reservations import counter_holds
		with hooks("gunshop_counter_holds", [f"{HERE}.holds_a", f"{HERE}.holds_b"]):
			out = counter_holds(self.ITEM)
		self.assertEqual(out["HOLD-A"]["ref"], "A-1")                # setdefault: A registered first
		self.assertEqual(out["HOLD-A"]["source"], "fake")
		self.assertEqual(out["HOLD-B"]["ref"], "B-2")
		self.assertEqual(set(out["HOLD-A"]), {"source", "ref", "kind", "tooltip", "url"})

	def test_failing_provider_is_logged_and_contributes_nothing(self):
		from ffl_core.reservations import counter_holds
		with hooks("gunshop_counter_holds", [f"{HERE}.holds_boom", f"{HERE}.holds_a"]):
			out = counter_holds(self.ITEM)
		self.assertIn("HOLD-A", out)                                  # the healthy provider still counts
		self.assertTrue(_error_logged("gunshop_counter_holds provider failed"))

	def test_the_counters_own_hold_beats_a_provider_on_the_same_serial(self):
		"""Providers are merged AFTER the counter sources (setdefault)."""
		from ffl_core import reservations
		with hooks("gunshop_counter_holds", [f"{HERE}.holds_a"]), \
				patch.object(reservations, "_draft_pos_serials", return_value=[("HOLD-A", "POS-INV-1")]):
			out = reservations.counter_holds(self.ITEM)
		self.assertEqual((out["HOLD-A"]["source"], out["HOLD-A"]["ref"]), ("pos", "POS-INV-1"))

	def test_no_providers_no_change(self):
		from ffl_core.reservations import counter_holds
		with hooks("gunshop_counter_holds", []):
			self.assertEqual(counter_holds(self.ITEM), {})


# --- gunshop_pending_queue_exclusion_fields ---------------------------------------
class TestPendingQueueExclusionFields(FrappeTestCase):
	def test_declared_column_joins_the_exclusions_when_it_exists(self):
		from ffl_core.compliance import channel_invoice
		with hooks("gunshop_pending_queue_exclusion_fields", ["remarks", "no_such_column_xyz"]):
			out = channel_invoice.pending_queue_exclusions()
			self.assertEqual(out.get("remarks"), ("is", "not set"))    # a real Sales Invoice column
			self.assertNotIn("no_such_column_xyz", out)               # column-guarded, never raises
			self.assertTrue(channel_invoice.is_fulfilled_elsewhere(frappe._dict(remarks="settled elsewhere")))
			self.assertFalse(channel_invoice.is_fulfilled_elsewhere(frappe._dict(remarks="")))
		# a channel mirror is fulfilled elsewhere regardless of the hook
		self.assertTrue(channel_invoice.is_fulfilled_elsewhere(frappe._dict(woo_online_order="WOO-1")))

	def test_dealer_dispositions_are_not_auto_booked_for_a_claimed_invoice(self):
		"""create_dealer_dispositions defers to whoever fulfilled the invoice —
		a second booking would double-push the gun to FastBound."""
		from ffl_core.compliance import dealer_transfer
		doc = frappe._dict(doctype="Sales Invoice", remarks="ext", update_stock=1)
		with hooks("gunshop_pending_queue_exclusion_fields", ["remarks"]), \
				patch.object(dealer_transfer, "_moves_stock", side_effect=AssertionError("looked past the claim")):
			dealer_transfer.create_dealer_dispositions(doc)

	def test_counter_order_marker_honours_the_hook(self):
		from ffl_core.api import manual_order
		doc = frappe._dict(remarks="ext", ffl_destination_ffl="1-23-456-07-8X-99999", pos_profile="")
		with hooks("gunshop_pending_queue_exclusion_fields", ["remarks"]):
			self.assertFalse(manual_order._is_counter_order(doc))
		with hooks("gunshop_pending_queue_exclusion_fields", []):
			self.assertTrue(manual_order._is_counter_order(doc))


# --- gunshop_disposition_stock_handled --------------------------------------------
class TestDispositionStockHandled(FrappeTestCase):
	def _doc(self):
		return frappe._dict(name="D-TEST", disposition_type="Dealer Transfer", stock_entry="")

	def test_true_stops_the_auto_issue(self):
		from ffl_core.compliance.disposition_stock import _should_issue_stock
		with hooks("gunshop_disposition_stock_handled", [f"{HERE}.stock_handled_yes"]):
			self.assertFalse(_should_issue_stock(self._doc()))

	def test_false_falls_through_to_the_type_rule(self):
		from ffl_core.compliance.disposition_stock import _should_issue_stock
		with hooks("gunshop_disposition_stock_handled", [f"{HERE}.stock_handled_no"]):
			self.assertTrue(_should_issue_stock(self._doc()))
			self.assertFalse(_should_issue_stock(frappe._dict(disposition_type="Retail Sale", stock_entry="")))

	def test_failing_provider_fails_closed(self):
		from ffl_core.compliance.disposition_stock import _should_issue_stock
		with hooks("gunshop_disposition_stock_handled", [f"{HERE}.stock_handled_boom"]):
			self.assertFalse(_should_issue_stock(self._doc()))
		self.assertTrue(_error_logged("gunshop_disposition_stock_handled failed"))


# --- gunshop_website_user_home_page ------------------------------------------------
class TestWebsiteUserHomePage(FrappeTestCase):
	WEB_USER = "hook-test-website-user@example.com"

	def setUp(self):
		super().setUp()
		if not frappe.db.exists("User", self.WEB_USER):
			frappe.get_doc({"doctype": "User", "email": self.WEB_USER, "first_name": "Hook",
				"user_type": "Website User", "send_welcome_email": 0}).insert(ignore_permissions=True)

	def test_first_answer_wins_for_a_website_user(self):
		from ffl_core import get_website_user_home_page
		with hooks("gunshop_website_user_home_page", [f"{HERE}.home_none", f"{HERE}.home_portal"]):
			self.assertEqual(get_website_user_home_page(self.WEB_USER), "fake-portal")

	def test_desk_user_never_reaches_providers(self):
		from ffl_core import get_website_user_home_page
		with hooks("gunshop_website_user_home_page", [f"{HERE}.home_portal"]):
			self.assertIsNone(get_website_user_home_page("Administrator"))

	def test_failing_provider_is_skipped(self):
		from ffl_core import get_website_user_home_page
		with hooks("gunshop_website_user_home_page", [f"{HERE}.home_boom", f"{HERE}.home_portal"]):
			self.assertEqual(get_website_user_home_page(self.WEB_USER), "fake-portal")
		self.assertTrue(_error_logged("gunshop_website_user_home_page failed"))


# --- gunshop_extension_toggled ------------------------------------------------------
from ffl_core.extensions import registry  # noqa: E402
from ffl_core.tests.test_extensions import FAKE, MANIFEST, ExtensionFixture  # noqa: E402


class TestExtensionToggled(ExtensionFixture):
	def setUp(self):
		super().setUp()
		TOGGLES.clear()

	def _flip(self, enabled: int):
		doc = frappe.get_doc("POS Extension", FAKE)
		doc.enabled = enabled
		doc.save()

	def test_providers_hear_both_directions_but_not_a_no_op_save(self):
		with hooks("gunshop_extension_toggled", [f"{HERE}.on_toggled"]):
			self._flip(0)
			self._flip(0)                                             # unchanged → no call
			self._flip(1)
		self.assertEqual(TOGGLES, [(FAKE, False), (FAKE, True)])

	def test_failing_provider_does_not_undo_the_switch(self):
		with hooks("gunshop_extension_toggled", [f"{HERE}.toggled_boom", f"{HERE}.on_toggled"]):
			self._flip(0)
		self.assertEqual(frappe.db.get_value("POS Extension", FAKE, "enabled"), 0)
		self.assertEqual(TOGGLES, [(FAKE, False)])                    # the next provider still ran
		self.assertTrue(_error_logged("gunshop_extension_toggled failed"))

	def test_a_compatibility_flip_is_heard_once_each_way(self):
		"""The effective switch changed without anyone touching ``enabled``."""
		with hooks("gunshop_extension_toggled", [f"{HERE}.on_toggled"]):
			self.manifest = {**MANIFEST, "requires_core": ">=9.0"}
			registry.sync()
			registry.sync()                                           # nothing changed → no second call
			self.manifest = MANIFEST
			registry.sync()
		self.assertEqual(TOGGLES, [(FAKE, False), (FAKE, True)])
		self.assertEqual(frappe.db.get_value("POS Extension", FAKE, "enabled"), 1)

	def test_install_and_uninstall_are_not_toggles(self):
		with hooks("gunshop_extension_toggled", [f"{HERE}.on_toggled"]):
			self.installed_here = False
			registry.sync()
			self.installed_here = True
			registry.sync()
		self.assertEqual(TOGGLES, [])

	def test_switching_an_extension_that_is_not_effective_is_not_a_toggle(self):
		self.manifest = {**MANIFEST, "requires_core": ">=9.0"}
		registry.sync()
		with hooks("gunshop_extension_toggled", [f"{HERE}.on_toggled"]):
			self._flip(0)
		self.assertEqual(TOGGLES, [])
