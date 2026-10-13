"""Tests for woocommerce/order_audit.py — the daily order-integrity audit (D2).

The webhook can silently swallow an order (handler exception → rollback → still
returns 200, so WooCommerce never retries). This scheduled audit is the backstop:
it pulls recent WooCommerce orders and flags any that SHOULD be in ERP but have
no Woo Online Order. Alert only — never auto-create.

Covered: the should-exist predicate, the missing-order set (pure), the per-site
glue (client + ERP lookup + alert), and the schedule gate (enabled + throttle).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, flt, now_datetime

from ffl_woo_sync.woocommerce import order_audit


def _wo(woo_id, status="completed"):
	return {"id": woo_id, "status": status, "number": str(woo_id), "total": "603.50"}


class TestShouldExist(FrappeTestCase):

	def test_live_statuses_should_exist(self):
		for status in ("processing", "completed", "on-hold"):
			self.assertTrue(order_audit._should_exist(_wo(1, status)), status)

	def test_release_statuses_should_not_exist(self):
		for status in ("cancelled", "refunded", "trash", "failed", "checkout-draft"):
			self.assertFalse(order_audit._should_exist(_wo(1, status)), status)


class TestMissingOrders(FrappeTestCase):

	def test_flags_a_live_order_absent_from_erp(self):
		missing = order_audit._missing_orders([_wo(1059)], existing_ids=[])
		self.assertEqual([o["id"] for o in missing], [1059])

	def test_does_not_flag_an_order_already_in_erp(self):
		missing = order_audit._missing_orders([_wo(1059)], existing_ids=["1059"])
		self.assertEqual(missing, [])

	def test_does_not_flag_a_terminal_order(self):
		missing = order_audit._missing_orders(
			[_wo(1782, "trash"), _wo(999, "cancelled")], existing_ids=[])
		self.assertEqual(missing, [])

	def test_ids_compared_as_strings(self):
		# ERP stores woo_order_id as Data; Woo sends int — must still match.
		missing = order_audit._missing_orders([_wo(1059)], existing_ids=[1059])
		self.assertEqual(missing, [])


class TestAuditSite(FrappeTestCase):

	def _patched(self, batch, existing):
		client = MagicMock()
		client.list_orders.return_value = batch  # len < 100 → single page
		return [
			patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
				return_value=client),
			patch("frappe.get_all", return_value=existing),
			patch("frappe.log_error"),
		]

	def test_alerts_once_per_missing_order_and_reports_ok(self):
		ctx = self._patched([_wo(1059), _wo(1060), _wo(999, "cancelled")],
			existing=["1060"])
		with ctx[0], ctx[1], ctx[2] as log:
			ok = order_audit._audit_site("retail")
		# 1059 missing+live → alert; 1060 present → skip; 999 cancelled → skip.
		self.assertTrue(ok)                 # ran to completion → caller may stamp
		self.assertEqual(log.call_count, 1)

	def test_no_missing_orders_no_alerts_still_ok(self):
		ctx = self._patched([_wo(1059)], existing=["1059"])
		with ctx[0], ctx[1], ctx[2] as log:
			ok = order_audit._audit_site("retail")
		self.assertTrue(ok)
		log.assert_not_called()

	def test_config_error_reports_not_ok_and_is_silent(self):
		from ffl_woo_sync.woocommerce.client import WooCommerceConfigError
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
				side_effect=WooCommerceConfigError("no creds")), \
			 patch("frappe.log_error") as log:
			ok = order_audit._audit_site("retail")
		self.assertFalse(ok)                # couldn't audit → caller must NOT stamp
		log.assert_not_called()

	def test_fetch_error_logs_and_reports_not_ok(self):
		# minor-2: an API failure must not read as "zero missing orders" — the
		# swallowed-order backstop must not itself swallow. log + don't stamp.
		from ffl_woo_sync.woocommerce.client import WooCommerceError
		client = MagicMock()
		client.list_orders.side_effect = WooCommerceError("503 Service Unavailable")
		with patch("ffl_woo_sync.woocommerce.client.WooCommerceClient.from_settings",
				return_value=client), \
			 patch("frappe.get_all", return_value=[]), \
			 patch("frappe.log_error") as log:
			ok = order_audit._audit_site("retail")
		self.assertFalse(ok)
		log.assert_called_once()


class TestGate(FrappeTestCase):

	def _settings(self, enabled=1, last_audit=None):
		s = MagicMock()
		s.auto_sync_enabled = enabled
		s.last_order_audit_at = last_audit
		return s

	def test_skips_when_auto_sync_disabled(self):
		with patch("frappe.get_single", return_value=self._settings(enabled=0)), \
			 patch.object(order_audit, "_audit_site") as audit, \
			 patch("frappe.db.set_value") as setval:
			order_audit._gate_and_audit("retail")
		audit.assert_not_called()
		setval.assert_not_called()

	def test_skips_when_not_due(self):
		from frappe.utils import now_datetime
		with patch("frappe.get_single",
				return_value=self._settings(enabled=1, last_audit=now_datetime())), \
			 patch.object(order_audit, "_audit_site") as audit:
			order_audit._gate_and_audit("retail")
		audit.assert_not_called()

	def test_runs_and_stamps_when_enabled_and_due(self):
		with patch("frappe.get_single",
				return_value=self._settings(enabled=1, last_audit=None)), \
			 patch.object(order_audit, "_audit_site", return_value=True) as audit, \
			 patch("frappe.db.set_value") as setval:
			order_audit._gate_and_audit("retail")
		audit.assert_called_once_with("retail")
		setval.assert_called_once()

	def test_does_not_stamp_when_audit_reports_failure(self):
		# minor-2: a failed fetch (audit → False) must NOT advance the throttle,
		# so the next cron retries instead of waiting a full day.
		with patch("frappe.get_single",
				return_value=self._settings(enabled=1, last_audit=None)), \
			 patch.object(order_audit, "_audit_site", return_value=False) as audit, \
			 patch("frappe.db.set_value") as setval:
			order_audit._gate_and_audit("retail")
		audit.assert_called_once_with("retail")
		setval.assert_not_called()


class TestWebRevenueBackstop(FrappeTestCase):
	"""The 2-hourly backstop: re-run the completion job of a Transferred order
	with no invoice, issue the stock leg of a web invoice whose accessory rows
	have no Delivery Note — and only ALERT where a person already acted."""

	def _run(self, *, unbooked=(), undelivered=(), cancelled=(), drafts=(),
			book=None, deliver=None):
		from ffl_woo_sync.woocommerce import order_audit
		si_docs = {}
		for n in undelivered:
			d = MagicMock()
			d.name = n
			d.woo_online_order = f"WOO-{n}"
			si_docs[n] = d

		def _get_value(doctype, filters=None, *a, **k):
			# two probes now: a dangling DRAFT is retried, a CANCELLED invoice is held.
			if not filters:
				return None
			if filters.get("docstatus") == 0:
				return f"SI-DRAFT-{filters['woo_online_order']}" \
					if filters["woo_online_order"] in drafts else None
			return f"SI-CANCELLED-{filters['woo_online_order']}" \
				if filters["woo_online_order"] in cancelled else None

		# The refund-breakdown sweep has its own tests (test_refunds).
		with patch.object(order_audit, "_sweep_pending_refund_breakdowns", return_value=([], [])), \
			 patch.object(order_audit, "_transferred_without_invoice", return_value=list(unbooked)), \
			 patch.object(order_audit, "_invoiced_without_delivery", return_value=list(undelivered)), \
			 patch("ffl_woo_sync.woocommerce.revenue.book_web_order_completion",
				side_effect=book or (lambda n: {"sales_invoice": f"SI-{n}", "status": "created"})) as booker, \
			 patch("ffl_woo_sync.woocommerce.revenue.deliver_web_stock_lines",
				side_effect=deliver or (lambda si: [f"DN-{si.name}"])) as leg, \
			 patch("ffl_core.alerts.alert_once") as once, \
			 patch("frappe.get_doc", side_effect=lambda dt, n: si_docs[n]), \
			 patch("frappe.db.get_value", side_effect=_get_value), \
			 patch.object(order_audit, "safe_commit") as commit, \
			 patch("frappe.db.rollback") as rollback, \
			 patch("frappe.log_error") as log, \
			 patch("frappe.get_traceback", return_value="tb"):
			result = order_audit.scheduled_web_revenue_backstop()
		return frappe._dict(result=result, booker=booker, leg=leg, once=once,
			commit=commit, rollback=rollback, log=log)

	def _titles(self, m):
		return [c.kwargs.get("title") for c in m.log.call_args_list]

	def test_books_each_unbooked_order_through_its_completion_job_and_logs_the_miss(self):
		m = self._run(unbooked=["WOO-ORD-01782", "WOO-ORD-01735"])
		self.assertEqual(m.result["booked"], ["WOO-ORD-01782", "WOO-ORD-01735"])
		self.assertEqual(m.result["failed"], [])
		self.assertEqual([c.args[0] for c in m.booker.call_args_list],
			["WOO-ORD-01782", "WOO-ORD-01735"])
		self.assertEqual(m.commit.call_count, 2)
		m.rollback.assert_not_called()
		self.assertIn("Web revenue backstop booked WOO-ORD-01782", self._titles(m))

	def test_an_order_the_primary_job_already_booked_is_not_reported_as_a_miss(self):
		# The row lock serialises an overlap: the second caller sees "exists".
		m = self._run(unbooked=["WOO-ORD-1"], book=lambda n: {"status": "exists"})
		self.assertEqual(m.result["booked"], [])
		self.assertEqual(self._titles(m), [])

	def test_an_antique_result_shape_is_read_through_its_revenue_key(self):
		m = self._run(unbooked=["WOO-ORD-ANT"],
			book=lambda n: {"issued": [], "revenue": {"status": "created"}})
		self.assertEqual(m.result["booked"], ["WOO-ORD-ANT"])

	def test_a_dangling_draft_is_retried_not_held(self):
		"""draft_total_mismatch leaves exactly this state; the booking entry point
		re-verifies and submits a dangling draft, so it must stay a candidate."""
		m = self._run(unbooked=["WOO-ORD-D"], drafts=["WOO-ORD-D"], cancelled=["WOO-ORD-D"])
		self.assertEqual(m.result["booked"], ["WOO-ORD-D"])
		self.assertEqual(m.result["held"], [])
		m.booker.assert_called_once()

	def test_a_cancelled_invoice_is_alerted_never_rebooked(self):
		m = self._run(unbooked=["WOO-ORD-X"], cancelled=["WOO-ORD-X"])
		self.assertEqual(m.result["held"], ["WOO-ORD-X"])
		self.assertEqual(m.result["booked"], [])
		m.booker.assert_not_called()
		m.once.assert_called_once()
		self.assertEqual(m.once.call_args.args[:2], ("Woo Online Order", "WOO-ORD-X"))
		self.assertIn("SI-CANCELLED-WOO-ORD-X", m.once.call_args.kwargs["message"])

	def test_a_failing_order_is_rolled_back_alerted_once_and_does_not_stop_the_run(self):
		def book(name):
			if name == "WOO-ORD-BAD":
				raise frappe.ValidationError("total mismatch")
			return {"status": "created"}

		m = self._run(unbooked=["WOO-ORD-BAD", "WOO-ORD-OK"], book=book)
		self.assertEqual(m.result["failed"], ["WOO-ORD-BAD"])
		self.assertEqual(m.result["booked"], ["WOO-ORD-OK"])
		m.rollback.assert_called_once()
		# alert_once (deduped on the order's ToDo), not a raw log per run
		self.assertEqual(m.once.call_args.args[:2], ("Woo Online Order", "WOO-ORD-BAD"))
		self.assertEqual(m.once.call_args.kwargs["title"], "Web revenue backstop failed: WOO-ORD-BAD")

	def test_issues_the_stock_leg_of_invoices_missing_a_delivery_note(self):
		m = self._run(undelivered=["ACC-SINV-00330"])
		self.assertEqual(m.result["delivered"], ["ACC-SINV-00330"])
		self.assertEqual(m.leg.call_args[0][0].name, "ACC-SINV-00330")
		m.commit.assert_called_once()
		self.assertIn("Web stock backstop delivered ACC-SINV-00330", self._titles(m))

	def test_a_stock_leg_that_could_not_issue_is_not_counted_as_delivered(self):
		# deliver_web_stock_lines never raises — it alerts and returns [] — so the
		# invoice simply stays a candidate for the next run.
		m = self._run(undelivered=["ACC-SINV-00331"], deliver=lambda si: [])
		self.assertEqual(m.result["delivered"], [])
		self.assertNotIn("Web stock backstop delivered ACC-SINV-00331", self._titles(m))

	def test_noop_when_nothing_is_owed(self):
		m = self._run()
		self.assertEqual(m.result, {"booked": [], "delivered": [], "failed": [], "held": [],
			"refund_breakdowns": [], "refund_breakdowns_pending": []})
		m.leg.assert_not_called()
		m.commit.assert_not_called()
		m.log.assert_not_called()

	def test_manual_entry_is_role_gated(self):
		from ffl_woo_sync.woocommerce import order_audit
		self.assertIn(order_audit.run_web_revenue_backstop_now, frappe.whitelisted)
		self.assertNotIn(order_audit.scheduled_web_revenue_backstop, frappe.whitelisted)


class TestWebStockRuleAgreement(FrappeTestCase):
	"""The SQL control and Python action rule over REAL rows — inserted raw, no
	validate/submit chain, exactly like ffl_core's sweep tests. Both agree on what
	stock is owed; human-owned draft/cancel/return rows deliberately remain in
	the SQL control while Python alerts and skips them. Rows roll back with the
	class transaction; every name is test-scoped."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from ffl_core.setup import item_groups, test_bootstrap
		test_bootstrap.run()
		item_groups.ensure()
		for code, group, is_firearm, is_stock in (
				("WSA-OPTIC", "Accessory", 0, 1), ("WSA-GUN", "Accessory", 1, 1),
				("WSA-LEGACY", "Pistol", 0, 1), ("WSA-SERVICE", "Accessory", 0, 0)):
			if not frappe.db.exists("Item", code):
				doc = {"doctype": "Item", "item_code": code, "item_name": code,
					"item_group": group, "stock_uom": "Nos", "is_stock_item": is_stock,
					"has_serial_no": is_firearm, "is_firearm": is_firearm}
				if is_firearm:
					doc["serial_no_series"] = f"{code}-.####"
				frappe.get_doc(doc).insert(ignore_permissions=True)

	def _si(self, tag, rows, *, update_stock=0, days_ago=1):
		name = f"WSA-{self._testMethodName}-{tag}"[:140]
		when = add_to_date(now_datetime(), days=-days_ago)
		frappe.db.delete("Sales Invoice Item", {"parent": name})
		frappe.db.delete("Sales Invoice", {"name": name})
		frappe.get_doc({
			"doctype": "Sales Invoice", "name": name, "docstatus": 1, "customer": "_Test Customer",
			"woo_online_order": f"WOO-{name}"[:140], "update_stock": update_stock,
			"posting_date": when.date(), "posting_time": when.time(), "set_posting_time": 1,
			"creation": when, "modified": when, "owner": "Administrator", "modified_by": "Administrator",
		}).db_insert()
		out = []
		for idx, (item, qty, warehouse, dropship) in enumerate(rows, start=1):
			rname = f"{name}-r{idx}"[:140]
			frappe.get_doc({
				"doctype": "Sales Invoice Item", "name": rname, "parent": name, "parenttype": "Sales Invoice",
				"parentfield": "items", "idx": idx, "docstatus": 1, "item_code": item, "qty": qty,
				"warehouse": warehouse, "delivered_by_supplier": dropship,
				"creation": when, "modified": when, "owner": "Administrator", "modified_by": "Administrator",
			}).db_insert()
			out.append(rname)
		return name, out

	def _dn_row(self, si_detail, qty, *, docstatus=1, is_return=0):
		dn = f"DN-{si_detail}"[:140]
		frappe.db.delete("Delivery Note Item", {"parent": dn})
		frappe.db.delete("Delivery Note", {"name": dn})
		now = now_datetime()
		frappe.get_doc({
			"doctype": "Delivery Note", "name": dn, "docstatus": docstatus, "is_return": is_return,
			"customer": "_Test Customer", "posting_date": now.date(),
			"creation": now, "modified": now, "owner": "Administrator", "modified_by": "Administrator",
		}).db_insert()
		frappe.get_doc({
			"doctype": "Delivery Note Item", "name": f"{dn}-row"[:140], "parent": dn,
			"parenttype": "Delivery Note", "parentfield": "items", "idx": 1, "docstatus": docstatus,
			"item_code": "WSA-OPTIC", "qty": qty, "si_detail": si_detail,
			"creation": now, "modified": now, "owner": "Administrator", "modified_by": "Administrator",
		}).db_insert()

	def _python_owed(self, si_name) -> set[str]:
		from ffl_woo_sync.woocommerce.revenue import _stock_leg_state, _web_stock_rows
		si = frappe.get_doc("Sales Invoice", si_name)
		rows = _web_stock_rows(si)
		delivered, blocked = _stock_leg_state([r.name for r in rows])
		return {r.name for r in rows
			if r.name not in blocked and flt(r.qty) - delivered.get(r.name, 0) > 0}

	def _sql_hit(self, si_name) -> bool:
		from ffl_woo_sync.woocommerce.order_audit import _invoiced_without_delivery
		return si_name in _invoiced_without_delivery(limit=500)

	def test_plain_accessory_is_owed_by_both(self):
		si, rows = self._si("owed", [("WSA-OPTIC", 2, "Stores - FT", 0)])
		self.assertTrue(self._sql_hit(si))
		self.assertEqual(self._python_owed(si), set(rows))

	def test_fully_delivered_row_is_owed_by_neither(self):
		si, rows = self._si("done", [("WSA-OPTIC", 2, "Stores - FT", 0)])
		self._dn_row(rows[0], 2)
		self.assertFalse(self._sql_hit(si))
		self.assertEqual(self._python_owed(si), set())

	def test_partially_delivered_row_is_still_owed_by_both(self):
		si, rows = self._si("part", [("WSA-OPTIC", 3, "Stores - FT", 0)])
		self._dn_row(rows[0], 1)
		self.assertTrue(self._sql_hit(si))
		self.assertEqual(self._python_owed(si), set(rows))

	def test_a_returned_row_stays_in_the_control_but_python_will_not_reissue(self):
		"""A human-owned row is still an outstanding accounting control item. The
		backstop hands it to Python so it can alert, but Python never reissues it."""
		si, rows = self._si("ret", [("WSA-OPTIC", 2, "Stores - FT", 0)])
		self._dn_row(rows[0], -2, is_return=1)
		self.assertTrue(self._sql_hit(si))
		self.assertEqual(self._python_owed(si), set())

	def test_a_draft_or_cancelled_dn_stays_in_the_control_but_is_not_reissued(self):
		si, rows = self._si("draft", [("WSA-OPTIC", 2, "Stores - FT", 0)])
		self._dn_row(rows[0], 2, docstatus=0)
		self.assertTrue(self._sql_hit(si))
		self.assertEqual(self._python_owed(si), set())
		si2, rows2 = self._si("canc", [("WSA-OPTIC", 2, "Stores - FT", 0)])
		self._dn_row(rows2[0], 2, docstatus=2)
		self.assertTrue(self._sql_hit(si2))
		self.assertEqual(self._python_owed(si2), set())

	def test_actionable_rows_sort_ahead_of_blocked_rows_at_the_batch_cap(self):
		"""Keeping blocked rows visible must not let them starve invoices the
		backstop can fix automatically."""
		from ffl_woo_sync.woocommerce.order_audit import _invoiced_without_delivery

		# Same isolated future day, and the blocked name sorts first alphabetically:
		# without the explicit blocked-last ordering a LIMIT 1 returns the wrong row.
		blocked, blocked_rows = self._si(
			"aaa-blocked", [("WSA-OPTIC", 1, "Stores - FT", 0)], days_ago=-10000)
		actionable, _ = self._si(
			"zzz-actionable", [("WSA-OPTIC", 1, "Stores - FT", 0)], days_ago=-10000)
		self._dn_row(blocked_rows[0], 1, docstatus=2)
		day = add_to_date(now_datetime(), days=10000).date()
		self.assertEqual(_invoiced_without_delivery(
			limit=1, from_date=day, to_date=day), [actionable])

	def test_the_month_end_window_bounds_posting_date(self):
		from ffl_woo_sync.woocommerce.order_audit import _invoiced_without_delivery
		si, _rows = self._si("month", [("WSA-OPTIC", 1, "Stores - FT", 0)], days_ago=1)
		day = add_to_date(now_datetime(), days=-1).date()
		self.assertIn(si, _invoiced_without_delivery(limit=500, from_date=day, to_date=day))
		self.assertNotIn(si, _invoiced_without_delivery(
			limit=500, from_date=add_to_date(now_datetime(), days=-9).date(),
			to_date=add_to_date(now_datetime(), days=-8).date()))

	def test_firearm_by_flag_and_by_legacy_group_are_excluded_by_both(self):
		si, _rows = self._si("guns", [("WSA-GUN", 1, "Stores - FT", 0), ("WSA-LEGACY", 1, "Stores - FT", 0)])
		self.assertFalse(self._sql_hit(si))
		self.assertEqual(self._python_owed(si), set())

	def test_non_stock_dropship_and_unplaced_rows_are_excluded_by_both(self):
		si, _rows = self._si("skip", [("WSA-SERVICE", 1, "Stores - FT", 0),
			("WSA-OPTIC", 1, "", 1), ("WSA-OPTIC", 1, "", 0)])
		self.assertFalse(self._sql_hit(si))
		self.assertEqual(self._python_owed(si), set())

	def test_a_stock_moving_invoice_is_excluded_by_both(self):
		from ffl_woo_sync.woocommerce.revenue import deliver_web_stock_lines
		si, _rows = self._si("moving", [("WSA-OPTIC", 2, "Stores - FT", 0)], update_stock=1)
		self.assertFalse(self._sql_hit(si))
		with patch("frappe.db.get_value") as lock:
			self.assertEqual(deliver_web_stock_lines(frappe.get_doc("Sales Invoice", si)), [])
		lock.assert_not_called()


class TestTransferredWindow(FrappeTestCase):
	"""``_transferred_without_invoice`` keys on when the order LEFT, not when it
	was placed — an order that waited a month for an FFL or a distributor
	restock must not fall out of the floor before it is ever fulfilled."""

	def _order(self, tag, *, created_days_ago, transferred_hours_ago=None,
			status="Transferred"):
		name = f"WT-{self._testMethodName}-{tag}"[:140]
		created = add_to_date(now_datetime(), days=-created_days_ago)
		frappe.db.delete("Woo Online Order", {"name": name})
		frappe.get_doc({
			"doctype": "Woo Online Order", "name": name, "woo_order_id": name,
			"order_status": status, "site": "retail", "customer_name": "Window Test",
			"transferred_at": None if transferred_hours_ago is None
				else add_to_date(now_datetime(), hours=-transferred_hours_ago),
			"creation": created, "modified": created,
			"owner": "Administrator", "modified_by": "Administrator",
		}).db_insert()
		return name

	def test_an_old_order_transferred_recently_is_a_candidate(self):
		from ffl_woo_sync.woocommerce.order_audit import _transferred_without_invoice
		name = self._order("late", created_days_ago=90, transferred_hours_ago=5)
		self.assertIn(name, _transferred_without_invoice(limit=500))

	def test_a_just_transferred_order_waits_out_the_settle_window(self):
		from ffl_woo_sync.woocommerce.order_audit import _transferred_without_invoice
		name = self._order("fresh", created_days_ago=90, transferred_hours_ago=0)
		self.assertNotIn(name, _transferred_without_invoice(limit=500))

	def test_an_unstamped_order_falls_back_to_creation(self):
		from ffl_woo_sync.woocommerce.order_audit import _transferred_without_invoice
		old = self._order("legacy-old", created_days_ago=90)
		recent = self._order("legacy-recent", created_days_ago=3)
		names = _transferred_without_invoice(limit=500)
		self.assertNotIn(old, names)      # outside the 30-day floor, as before
		self.assertIn(recent, names)

	def test_a_pending_order_is_never_a_candidate(self):
		from ffl_woo_sync.woocommerce.order_audit import _transferred_without_invoice
		name = self._order("pending", created_days_ago=3, status="Pending Fulfillment")
		self.assertNotIn(name, _transferred_without_invoice(limit=500))


class TestMonthEndControl(FrappeTestCase):

	def test_the_listing_endpoint_is_role_gated_and_read_only(self):
		from ffl_woo_sync.woocommerce import order_audit
		self.assertIn(order_audit.web_stock_legs_outstanding, frappe.whitelisted)
		with patch.object(order_audit, "_invoiced_without_delivery",
				return_value=["ACC-SINV-1"]) as q, \
			 patch("frappe.only_for") as gate:
			out = order_audit.web_stock_legs_outstanding(
				from_date="2026-08-01", to_date="2026-08-31")
		gate.assert_called_once()
		self.assertEqual(out, {"invoices": ["ACC-SINV-1"], "count": 1})
		self.assertEqual(q.call_args.kwargs["from_date"], "2026-08-01")
		self.assertEqual(q.call_args.kwargs["to_date"], "2026-08-31")
