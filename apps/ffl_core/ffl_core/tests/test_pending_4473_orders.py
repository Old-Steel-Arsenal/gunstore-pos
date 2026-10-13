"""Pending 4473 Orders report — the NICS-parked-order dashboard.

Parked firearm sales (4473 pending / NICS Delayed) live as Draft POS Invoices
with nothing tracking them; this report is the queue the operator checks every
morning: every draft firearm order, how long it's been parked, and the Brady
3rd-business-day date after which a Delayed transfer may proceed at the
dealer's discretion.
"""
from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase


class BradyDate(FrappeTestCase):
	def test_add_business_days_skips_weekend(self):
		from ffl_core.ffl_core.report.pending_4473_orders.pending_4473_orders import (
			add_business_days,
		)
		# Fri 2026-06-05 + 3 business days → Wed 2026-06-10
		self.assertEqual(add_business_days(date(2026, 6, 5), 3), date(2026, 6, 10))
		# Mon 2026-06-08 + 3 business days → Thu 2026-06-11
		self.assertEqual(add_business_days(date(2026, 6, 8), 3), date(2026, 6, 11))
		# Sat start rolls through to Wed
		self.assertEqual(add_business_days(date(2026, 6, 6), 3), date(2026, 6, 10))


class Pending4473Fixtures:
	"""Shared fixtures + helpers WITHOUT test methods (a firearm Item, an
	accessory Item, a customer, an open POS shift, and a draft builder). Modules
	that need draft firearm orders subclass this alongside FrappeTestCase —
	inheriting Pending4473Orders instead would re-run its tests once per subclass
	(unittest collects inherited test_ methods; the BuildOrderMixin rule)."""

	@classmethod
	def setUpClass(cls) -> None:
		super().setUpClass()
		from ffl_core.setup import dev_site
		dev_site.run()
		cls.company = frappe.db.get_value("Company", {"company_name": "FFL Test"}, "name")
		frappe.defaults.set_user_default("Company", cls.company)
		# Fresh CI sites have no Customer at all — create ours explicitly.
		cls.customer = "_Test P4473 Buyer"
		if not frappe.db.exists("Customer", cls.customer):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": cls.customer,
				"customer_group": "Individual", "territory": "All Territories",
			}).insert(ignore_permissions=True)
		from ffl_core.tests.pos_opening import todays_pos_opening
		todays_pos_opening(cls.company)
		if not frappe.db.exists("Item", "P4473-GUN"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "P4473-GUN", "item_name": "P4473-GUN",
				"item_group": "Firearm", "stock_uom": "Nos",
				"is_stock_item": 1, "has_serial_no": 1,
				"serial_no_series": "P4473-GUN-.####",
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Item", "P4473-ACC"):
			frappe.get_doc({
				"doctype": "Item", "item_code": "P4473-ACC", "item_name": "P4473-ACC",
				"item_group": "Accessory", "stock_uom": "Nos",
				"is_stock_item": 0, "has_serial_no": 0,
			}).insert(ignore_permissions=True)

	def _draft_invoice(self, item_code: str, *, serial: str = "",
			nics: str = "", form_4473: str = "") -> str:
		inv = frappe.new_doc("POS Invoice")
		inv.pos_profile = "FFL Test"
		inv.customer = self.customer
		inv.append("items", {"item_code": item_code, "qty": 1, "rate": 600,
			"price_list_rate": 600, "serial_no": serial,
			"use_serial_batch_fields": 1, "allow_zero_valuation_rate": 1})
		inv.set_missing_values()
		if form_4473:
			inv.form_4473_status = form_4473
		if nics:
			inv.nics_status = nics
		inv.flags.ignore_permissions = True
		inv.save()
		return inv.name

	def _bundle_draft(self, serial: str) -> tuple[str, str]:
		"""A draft whose firearm line stores its serial in a Serial and Batch
		Bundle — the v16 DEFAULT — instead of the `serial_no` string every other
		fixture here writes.

		The bundle is inserted with validation off (it wants live stock and a
		posting voucher, neither of which a report reads) and the saved line is
		then rewritten into the bundle shape, because saving it that way round
		would pull ERPNext's bundle validation into a report test.
		"""
		if not frappe.db.exists("Serial No", serial):
			sn = frappe.get_doc({"doctype": "Serial No", "serial_no": serial,
				"item_code": "P4473-GUN", "company": self.company})
			sn.flags.ignore_permissions = True
			sn.insert()
		bundle = frappe.get_doc({
			"doctype": "Serial and Batch Bundle",
			"company": self.company, "item_code": "P4473-GUN", "has_serial_no": 1,
			"type_of_transaction": "Outward", "voucher_type": "POS Invoice",
			"entries": [{"serial_no": serial, "qty": -1}],
		})
		bundle.flags.ignore_validate = True
		bundle.flags.ignore_permissions = True
		bundle.insert(ignore_permissions=True, ignore_mandatory=True, ignore_links=True)
		name = self._draft_invoice("P4473-GUN", form_4473="Pending")
		row = frappe.get_doc("POS Invoice", name).items[0].name
		frappe.db.set_value("POS Invoice Item", row, {
			"serial_no": None, "use_serial_batch_fields": 0,
			"serial_and_batch_bundle": bundle.name}, update_modified=False)
		return name, bundle.name

	def _drop_bundle_draft(self, name: str, bundle: str) -> None:
		"""Invoice line FIRST, then the bundle.

		IntegrationTestCase rolls back once per CLASS, not per test, so an invoice
		left behind here is visible to every later test in it — and a line still
		pointing at a deleted bundle makes `serials_for_line` raise
		DoesNotExistError for all of them.
		"""
		frappe.db.delete("POS Invoice Item", {"parent": name})
		frappe.db.delete("POS Invoice", {"name": name})
		frappe.db.delete("Serial and Batch Entry", {"parent": bundle})
		frappe.db.delete("Serial and Batch Bundle", {"name": bundle})


class Pending4473Orders(Pending4473Fixtures, FrappeTestCase):
	def _run(self, filters=None):
		from ffl_core.ffl_core.report.pending_4473_orders.pending_4473_orders import execute
		_, rows = execute(filters or {})
		return rows

	def test_draft_firearm_order_is_listed_with_aging_and_brady(self):
		name = self._draft_invoice("P4473-GUN", serial="P4473-S1",
			form_4473="Completed", nics="Delayed")
		rows = self._run()
		mine = [r for r in rows if r["pos_invoice"] == name]
		self.assertEqual(len(mine), 1)
		row = mine[0]
		self.assertEqual(row["nics_status"], "Delayed")
		self.assertEqual(row["days_pending"], 0)
		self.assertIn("P4473-S1", row["serials"] or "")
		self.assertTrue(row["brady_date"])  # 3 business days out

	def test_a_bundle_line_shows_its_gun(self):
		"""The queue aggregated `GROUP_CONCAT(pii.serial_no)`, which is NULL for a
		v16 line: the Firearms column came out blank and the FastBound probe had
		no serial to ask about — on what is now the default storage model. Serials
		are resolved through ffl_core.serials.serials_for_line instead."""
		name, bundle = self._bundle_draft("P4473-BUNDLE-1")
		try:
			rows = self._run()
			mine = [r for r in rows if r["pos_invoice"] == name]
			self.assertEqual(len(mine), 1)
			self.assertEqual(mine[0]["serials"], "P4473-BUNDLE-1")
		finally:
			self._drop_bundle_draft(name, bundle)

	def test_a_bundle_line_is_what_the_fastbound_probe_is_asked_about(self):
		"""A blank Firearms column also silently emptied the FB Pending Disp.
		column — the investigation tool for the very incident this queue serves."""
		from ffl_core.ffl_core.report.pending_4473_orders import pending_4473_orders as rpt
		name, bundle = self._bundle_draft("P4473-BUNDLE-2")
		try:
			with patch.object(rpt, "fb_pending_dispositions",
					return_value={"P4473-BUNDLE-2": "pending-9 (item #00843)"}) as probe:
				_, rows = rpt.execute({"check_fastbound": 1})
			self.assertIn("P4473-BUNDLE-2", probe.call_args[0][0])
			mine = [r for r in rows if r["pos_invoice"] == name]
			self.assertEqual(mine[0]["fb_pending_disposition"],
				"P4473-BUNDLE-2: pending-9 (item #00843)")
		finally:
			self._drop_bundle_draft(name, bundle)

	def test_accessory_only_draft_not_listed(self):
		name = self._draft_invoice("P4473-ACC")
		rows = self._run()
		self.assertFalse([r for r in rows if r["pos_invoice"] == name])

	def test_nics_status_filter(self):
		delayed = self._draft_invoice("P4473-GUN", serial="P4473-S2",
			form_4473="Completed", nics="Delayed")
		pending = self._draft_invoice("P4473-GUN", serial="P4473-S3",
			form_4473="Pending")
		rows = self._run({"nics_status": "Delayed"})
		names = {r["pos_invoice"] for r in rows}
		self.assertIn(delayed, names)
		self.assertNotIn(pending, names)


# ===========================================================================
# DISP-2026-02000 — the parked order nobody came back for
# ===========================================================================

MOD = "ffl_core.api.pending_4473"


def _report_row(**over):
	base = {
		"pos_invoice": "PSINV-AB-1", "parked_on": date(2026, 6, 5),
		"days_pending": 6, "customer": "Buyer", "serials": "SN-1",
		"form_4473_status": "Pending", "nics_status": "Delayed",
		# Fri 2026-06-05 + 3 business days = Wed 2026-06-10.
		"brady_date": date(2026, 6, 10), "grand_total": 600.0, "is_ffl_transfer": 0,
	}
	base.update(over)
	return base


class OverduePending4473(FrappeTestCase):
	"""The threshold is Brady + 2 business days: the Brady date
	is the anchor the operator already reads off this queue, so the clock is hung
	off it rather than off a second constant nobody would keep in step."""

	def _overdue(self, rows, as_of):
		from ffl_core.api import pending_4473

		with patch.object(pending_4473, "pending_4473_report",
				return_value=([], rows)):
			return pending_4473.overdue_pending_4473(as_of)

	def test_two_business_days_past_brady_is_still_in_time(self):
		# Brady Wed 6/10 + 2 business days = Fri 6/12; on 6/12 itself it is not
		# yet late.
		self.assertEqual(self._overdue([_report_row()], date(2026, 6, 12)), [])

	def test_the_day_after_is_abandoned(self):
		rows = self._overdue([_report_row()], date(2026, 6, 15))
		self.assertEqual([r["pos_invoice"] for r in rows], ["PSINV-AB-1"])
		self.assertEqual(rows[0]["due_date"], date(2026, 6, 12))

	def test_the_grace_counts_business_days_not_calendar_days(self):
		"""Brady Fri 6/12 + 2 business days is Tue 6/16, not Sun 6/14 — a weekend
		must not turn a Monday-morning order into an abandoned one."""
		rows = self._overdue([_report_row(brady_date=date(2026, 6, 12))],
			date(2026, 6, 15))
		self.assertEqual(rows, [])

	def test_only_a_PENDING_4473_ages_out(self):
		"""A completed 4473 waiting out a NICS delay is a slow order, not an
		abandoned one — the customer is coming back for it."""
		rows = self._overdue(
			[_report_row(form_4473_status="Completed")], date(2026, 6, 30))
		self.assertEqual(rows, [])


class AlertOverduePending4473(FrappeTestCase):
	def _run(self, rows, *, already_alerted=False):
		from ffl_core.api import pending_4473

		with patch.object(pending_4473, "overdue_pending_4473", return_value=rows), \
			 patch("ffl_core.alerts.alert_once",
				 return_value=not already_alerted) as alert:
			return pending_4473.alert_overdue_pending_4473(), alert

	def test_an_abandoned_order_raises_one_alert_against_its_invoice(self):
		out, alert = self._run([{**_report_row(), "due_date": date(2026, 6, 12)}])
		self.assertEqual(out, {"overdue": 1, "alerted": 1})
		self.assertEqual(alert.call_args[0], ("POS Invoice", "PSINV-AB-1"))
		self.assertIn("SN-1", alert.call_args[1]["message"])

	def test_an_order_already_carrying_an_open_todo_is_not_re_alerted(self):
		"""The condition is permanent until a person acts on it; one ToDo per day
		is how an alert becomes noise nobody reads."""
		out, _ = self._run([{**_report_row(), "due_date": date(2026, 6, 12)}],
			already_alerted=True)
		self.assertEqual(out, {"overdue": 1, "alerted": 0})

	def test_a_clean_queue_alerts_nothing(self):
		out, alert = self._run([])
		self.assertEqual(out, {"overdue": 0, "alerted": 0})
		alert.assert_not_called()


class NoCronDeletesAnAbandoned4473(FrappeTestCase):
	"""FastBound's leftover pending disposition is
	removed by a MANAGER pressing a button with a reason attached — never by a
	timer. Deleting a bound-book record is ATF-visible, and a cron cannot hold
	the judgement about whether the customer is still coming back.

	One scheduled task does call DELETE /Dispositions and stays:
	`reconcile_failed_deletes` retries the delete of OUR OWN disposition, one a
	human already cancelled in POS. It is scoped to docstatus=2 records carrying
	a recorded 'DELETE failed', so it can never reach a disposition FastBound's
	own 4473 wizard created — there is no cancelled POS record pointing at one.
	"""

	def _scheduled_paths(self) -> list[str]:
		paths: list[str] = []
		for app in ("ffl_core", "ffl_integrations", "ffl_woo_sync"):
			events = getattr(
				frappe.get_module(f"{app}.hooks"), "scheduler_events", {})
			stack = [events]
			while stack:
				cur = stack.pop()
				if isinstance(cur, str):
					paths.append(cur)
				elif isinstance(cur, dict):
					stack.extend(cur.values())
				elif isinstance(cur, (list, tuple)):
					stack.extend(cur)
		return paths

	def test_the_cleanup_is_not_on_any_schedule(self):
		joined = "\n".join(self._scheduled_paths())
		self.assertNotIn("manager_abandon_4473", joined)
		self.assertNotIn("pos_4473_recovery", joined)

	def test_the_watch_that_IS_scheduled_only_alerts(self):
		"""Behavioural, not a grep: run it over an abandoned order and watch it
		neither reach FastBound nor delete anything."""
		from ffl_core.api import pending_4473

		with patch.object(pending_4473, "overdue_pending_4473",
				return_value=[{**_report_row(), "due_date": date(2026, 6, 12)}]), \
			 patch("ffl_core.alerts.alert_once", return_value=True), \
			 patch("frappe.delete_doc") as delete_doc, \
			 patch("frappe.get_doc") as get_doc:
			pending_4473.alert_overdue_pending_4473()
		delete_doc.assert_not_called()
		get_doc.assert_not_called()

	def test_the_only_scheduled_deleter_stays_scoped_to_cancelled_pos_records(self):
		"""If that scope ever widens, the rule above is quietly gone."""
		import inspect

		from ffl_integrations.fastbound import tasks

		src = inspect.getsource(tasks.reconcile_failed_deletes)
		self.assertIn('"docstatus": 2', src)
		self.assertIn("DELETE failed%", src)


class FastBoundPendingColumn(FrappeTestCase):
	"""The other half of DISP-2026-02000: the abandoned wizard's disposition is
	invisible from POS — the bound book still reads available — until the gun's
	next real disposition 400s against it."""

	def _rows(self, filters, probe):
		from ffl_core.ffl_core.report.pending_4473_orders import pending_4473_orders as rpt

		# One row per firearm LINE now (the serials are resolved in Python so both
		# storage models are covered — see _rows in the report).
		line = {"pos_invoice": "PSINV-FB-1", "posting_date": date(2026, 6, 5),
			"customer": "B", "form_4473_status": "Pending", "nics_status": "",
			"is_ffl_transfer": 0, "grand_total": 600.0,
			"serial_and_batch_bundle": None}
		# frappe._dict, like a real as_dict=True row: serials_for_line tests with
		# .get() and then READS with attribute access.
		lines = [frappe._dict({**line, "serial_no": "SN-1"}),
			frappe._dict({**line, "serial_no": "SN-2"})]
		# Answer the REPORT's query and pass everything else through. A blanket
		# `frappe.db.sql` stub also answers the framework's own reads — a cold meta
		# cache loads System Settings through the ORM mid-report and got handed
		# these rows as property setters.
		real_sql = frappe.db.sql

		def sql(query, *args, **kwargs):
			if "tabPOS Invoice Item" in str(query):
				return lines
			return real_sql(query, *args, **kwargs)

		with patch("frappe.db.sql", side_effect=sql), \
			 patch.object(rpt, "fb_pending_dispositions", side_effect=probe) as p:
			_, rows = rpt.execute(filters)
		return rows, p

	def test_the_column_is_blank_and_fastbound_is_untouched_by_default(self):
		"""One round trip per gun on a queue the operator opens every morning is
		not a default anybody wants."""
		rows, probe = self._rows({}, lambda _s: {})
		probe.assert_not_called()
		self.assertEqual(rows[0]["fb_pending_disposition"], "")

	def test_asking_shows_which_gun_is_held_and_by_what(self):
		rows, _ = self._rows({"check_fastbound": 1},
			lambda _s: {"SN-2": "pending-disp-9 (Pending)"})
		self.assertEqual(rows[0]["fb_pending_disposition"],
			"SN-2: pending-disp-9 (Pending)")

	def test_a_probe_that_answers_nothing_leaves_the_column_blank(self):
		rows, _ = self._rows({"check_fastbound": 1}, lambda _s: {})
		self.assertEqual(rows[0]["fb_pending_disposition"], "")

	def _probe(self, found):
		from ffl_core.ffl_core.report.pending_4473_orders import pending_4473_orders as rpt

		with patch("frappe.get_hooks", return_value=["x.y.probe"]), \
			 patch("frappe.get_attr", return_value=lambda _s: found):
			return rpt.fb_pending_dispositions(["SN-1"])

	def test_the_column_renders_the_item_number_the_probe_sends(self):
		"""Contract test for the two ends. The column used to read a `status` key
		the only registered probe never sent — dead code — while dropping
		`item_number`, which is the number FastBound's own 400 names and the one an
		operator types into its search box to reach the hold."""
		self.assertEqual(
			self._probe({"SN-1": {"disposition_id": "pending-9", "item_number": "00843"}}),
			{"SN-1": "pending-9 (item #00843)"})

	def test_a_hold_with_no_item_number_still_names_its_disposition(self):
		self.assertEqual(
			self._probe({"SN-1": {"disposition_id": "pending-9", "item_number": ""}}),
			{"SN-1": "pending-9"})

	def test_a_failing_probe_costs_the_column_and_not_the_queue(self):
		"""FastBound being down must never take away the morning queue."""
		from ffl_core.ffl_core.report.pending_4473_orders import pending_4473_orders as rpt

		def boom(_serials):
			raise RuntimeError("FastBound is down")

		with patch("frappe.get_hooks", return_value=["x.y.boom"]), \
			 patch("frappe.get_attr", return_value=boom), \
			 patch("frappe.log_error") as log:
			self.assertEqual(rpt.fb_pending_dispositions(["SN-1"]), {})
		log.assert_called_once()


class CleanupButtonIsManagerOnly(FrappeTestCase):
	"""The report's own front end. Source-level, the same shape as
	test_pending_dispositions_front_end_invariants.py: this repo ships no JS test
	runner, and `onload` is not reachable from Python.

	The server gate is the security boundary and is tested for real in
	ffl_integrations' test_pos_4473_recovery.test_abandon_requires_manager_role_
	and_reason. What is pinned here is that a cashier is not OFFERED a button
	whose only possible ending is a PermissionError after they have picked an
	invoice and typed a reason — and that the two lists of roles cannot drift
	apart.
	"""

	@staticmethod
	def _onload() -> str:
		from ffl_core.tests.front_end_invariants import block, code_only, read

		js = read("ffl_core", "ffl_core", "report", "pending_4473_orders",
			"pending_4473_orders.js")
		return code_only(block(js, "\tonload(report) {", "\t"))

	def test_the_role_check_comes_BEFORE_the_button_is_added(self):
		body = self._onload()
		self.assertIn("frappe.user.has_role", body)
		self.assertLess(
			body.index("frappe.user.has_role"), body.index("add_inner_button"),
			msg="check the role first — a button added and then hidden is not hidden")

	def test_the_button_and_the_server_gate_name_the_SAME_roles(self):
		"""`_check_manager_perm` is the gate; a client list that drifts from it
		either hides the button from somebody allowed to use it or puts it back in
		front of somebody who is not."""
		import inspect

		from ffl_integrations.fastbound.pos_4473 import _check_manager_perm

		server = inspect.getsource(_check_manager_perm)
		body = self._onload()
		for role in ("Sales Manager", "System Manager"):
			self.assertIn(role, server)
			self.assertIn(role, body)


if __name__ == "__main__":
	unittest.main()
