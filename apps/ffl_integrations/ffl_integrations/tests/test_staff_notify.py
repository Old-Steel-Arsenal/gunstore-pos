"""Run B — the four in-app staff notifications (desk bell, Notification Log type Alert).

Each event reaches the roles it is meant for and nobody else, rings once however
often it is redelivered, sends no email, and a failure to ring never costs the
webhook / push / poll / insert that raised it.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, now_datetime

SALES = "notify-sales@example.com"  # Sales User: the counter
STOCK = "notify-stock@example.com"  # Stock Manager: receives, never runs a 4473
BOOKS = "notify-books@example.com"  # Accounts User: on none of the lists
STAFF = (SALES, STOCK, BOOKS)


def _user(email: str, role: str) -> None:
	if frappe.db.exists("User", email):
		return
	user = frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0],
		"send_welcome_email": 0, "user_type": "System User"})
	user.insert(ignore_permissions=True)
	user.add_roles(role)


def _bell(user: str, doctype: str, name: str) -> list[str]:
	return frappe.get_all("Notification Log", filters={"for_user": user,
		"document_type": doctype, "document_name": name}, pluck="subject", order_by="creation")


def _bare(doctype: str, name: str, **fields) -> str:
	"""A row with just the fields the code under test reads — no controller, no
	opening shift, no bound book."""
	doc = frappe.new_doc(doctype)
	doc.update({"name": name, **fields})
	doc.db_insert()
	return name


class _Base(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_user(SALES, "Sales User")
		_user(STOCK, "Stock Manager")
		_user(BOOKS, "Accounts User")

	def assertRang(self, doctype, name, expected: dict[str, int]):
		got = {u: len(_bell(u, doctype, name)) for u in STAFF}
		self.assertEqual(got, {u: expected.get(u, 0) for u in STAFF})


class NicsVerdicts(_Base):
	def _complete_4473(self, invoice: str, response: int) -> None:
		from ffl_integrations.fastbound import webhook as fb
		with patch.object(fb, "_authoritative_4473_serials", return_value=["NTFY-S1"]), \
				patch.object(fb, "_find_draft_invoices_for_serials", return_value={invoice: ["NTFY-S1"]}), \
				patch.object(fb, "_record_4473_log"), patch.object(fb, "_ensure_disposition_coverage"):
			fb._handle_form4473_completed({"id": 1, "disposition": {"id": "FB-NTFY"}, "response": response})

	def test_delayed_then_cleared_rings_the_4473_counter_twice_and_a_redelivery_not_at_all(self):
		inv = _bare("POS Invoice", "NTFY-INV-1", customer_name="Jane <b>Buyer</b>")
		self._complete_4473(inv, 1)  # Delayed
		self.assertEqual(frappe.db.get_value("POS Invoice", inv, "nics_status"), "Delayed")
		self.assertRang("POS Invoice", inv, {SALES: 1})
		self._complete_4473(inv, 2)  # Proceed
		self._complete_4473(inv, 2)  # redelivered
		self.assertRang("POS Invoice", inv, {SALES: 2})
		self.assertEqual(_bell(SALES, "POS Invoice", inv), [
			"NICS Delayed — Jane &lt;b&gt;Buyer&lt;/b&gt; (NTFY-INV-1)",
			"Delayed NICS cleared: Proceed — Jane &lt;b&gt;Buyer&lt;/b&gt; (NTFY-INV-1)",
		])

	def test_a_delayed_check_that_ends_denied_is_never_called_cleared(self):
		inv = _bare("POS Invoice", "NTFY-INV-DD", customer_name="Buyer")
		self._complete_4473(inv, 1)  # Delayed
		self._complete_4473(inv, 3)  # Denied
		self.assertEqual(_bell(SALES, "POS Invoice", inv), [
			"NICS Delayed — Buyer (NTFY-INV-DD)", "NICS Denied — Buyer (NTFY-INV-DD)"])

	def test_proceed_and_denied_ring_and_a_voided_4473_does_not(self):
		for name, response, rings in (("NTFY-INV-P", 2, 1), ("NTFY-INV-D", 3, 1), ("NTFY-INV-V", 4, 0)):
			inv = _bare("POS Invoice", name, customer_name="Buyer")
			self._complete_4473(inv, response)
			self.assertRang("POS Invoice", inv, {SALES: rings})

	def test_a_delayed_check_cleared_by_disposition_committed_rings(self):
		from ffl_integrations.fastbound import webhook as fb
		inv = _bare("POS Invoice", "NTFY-INV-C", customer_name="Buyer",
			nics_status="Delayed", fastbound_disposition_id="FB-NTFY-C")
		with patch.object(fb, "_sync_fb_direct_disposal_stock"):
			fb._handle_disposition_committed({"dispositionId": "FB-NTFY-C"})
		self.assertEqual(frappe.db.get_value("POS Invoice", inv, "nics_status"), "Proceed")
		self.assertEqual(_bell(SALES, "POS Invoice", inv), ["Delayed NICS cleared: Proceed — Buyer (NTFY-INV-C)"])

	def test_a_committed_disposition_never_announces_a_denied_buyer_as_cleared(self):
		from ffl_integrations.fastbound import webhook as fb
		inv = _bare("POS Invoice", "NTFY-INV-CD", customer_name="Buyer",
			nics_status="Denied", fastbound_disposition_id="FB-NTFY-CD")
		with patch.object(fb, "_sync_fb_direct_disposal_stock"):
			fb._handle_disposition_committed({"dispositionId": "FB-NTFY-CD"})
		self.assertRang("POS Invoice", inv, {})

	def test_no_email_and_a_failed_bell_still_records_the_verdict(self):
		before = frappe.db.count("Email Queue Recipient", {"recipient": ("in", STAFF)})
		inv = _bare("POS Invoice", "NTFY-INV-E", customer_name="Buyer")
		self._complete_4473(inv, 1)
		self.assertRang("POS Invoice", inv, {SALES: 1})
		self.assertEqual(frappe.db.count("Email Queue Recipient", {"recipient": ("in", STAFF)}), before)

		inv = _bare("POS Invoice", "NTFY-INV-F", customer_name="Buyer")
		with patch("ffl_core.alerts.alert_assignees", side_effect=RuntimeError("boom")):
			self._complete_4473(inv, 1)
		self.assertEqual(frappe.db.get_value("POS Invoice", inv, "nics_status"), "Delayed")
		self.assertTrue(frappe.db.exists("Error Log", {"method": f"Staff notification failed: POS Invoice {inv}"}))


class NewOrders(_Base):
	def _woo(self, **fields):
		doc = frappe.get_doc({"doctype": "Woo Online Order", "woo_order_id": f"NTFY-{frappe.generate_hash(length=8)}",
			"order_status": "Pending Fulfillment", "site": "retail", "customer_name": "Web Buyer",
			"order_total": 1299, "woo_status": "processing", **fields})
		doc.flags.ignore_permissions = True
		return doc.insert()

	def _gb(self, **fields):
		doc = frappe.get_doc({"doctype": "GunBroker Order", "gb_order_id": frappe.generate_hash(length=10),
			"order_status": "Pending Fulfillment", "buyer_username": "bidder1", "order_total": 450,
			"order_date": now_datetime(), **fields})
		doc.flags.ignore_permissions = True
		return doc.insert()

	def test_a_web_order_rings_the_counter(self):
		doc = self._woo()
		self.assertRang("Woo Online Order", doc.name, {SALES: 1, STOCK: 1})
		self.assertEqual(_bell(SALES, "Woo Online Order", doc.name),
			[f"New web order #{doc.woo_order_id} — Web Buyer · $1,299.00"])
		doc.save()  # later edits are not new orders
		self.assertRang("Woo Online Order", doc.name, {SALES: 1, STOCK: 1})

	def test_a_gunbroker_order_rings_but_a_backfilled_or_cancelled_one_does_not(self):
		doc = self._gb()
		self.assertEqual(_bell(STOCK, "GunBroker Order", doc.name),
			[f"New GunBroker order #{doc.gb_order_id} — bidder1 · $450.00"])
		self.assertRang("GunBroker Order", doc.name, {SALES: 1, STOCK: 1})
		for old in (self._gb(order_date=add_days(now_datetime(), -30)), self._gb(cancel_date=now_datetime())):
			self.assertRang("GunBroker Order", old.name, {})

	def test_an_unpaid_woo_order_rings_once_it_is_paid(self):
		doc = self._woo(woo_status="pending")
		self.assertRang("Woo Online Order", doc.name, {})
		doc.woo_status = "processing"
		doc.save()
		doc.order_total = 1350
		doc.save()  # a later edit is not a new sale
		self.assertRang("Woo Online Order", doc.name, {SALES: 1, STOCK: 1})

	def test_a_finished_woo_order_new_to_pos_does_not_ring(self):
		doc = self._woo(woo_status="completed")
		self.assertRang("Woo Online Order", doc.name, {})

	def test_a_bell_queued_for_a_rolled_back_order_does_not_ring_on_the_order_that_reused_its_name(self):
		from ffl_core.alerts import notify_staff
		from ffl_core.roles import COUNTER_ROLES
		doc = self._gb()
		notify_staff("GunBroker Order", doc.name, "New GunBroker order #rolled-back", COUNTER_ROLES,
			match={"gb_order_id": "rolled-back"})
		self.assertEqual(len(_bell(SALES, "GunBroker Order", doc.name)), 1)  # only the real order's

	def test_a_failed_bell_never_blocks_the_order(self):
		with patch("ffl_core.alerts.notify_staff", side_effect=RuntimeError("boom")):
			doc = self._woo()
		self.assertTrue(frappe.db.exists("Woo Online Order", doc.name))
		self.assertTrue(frappe.db.exists("Error Log", {"method": f"New-order notification failed: {doc.name}"}))


class TerminalBookFailures(_Base):
	def _push(self, name: str, error: str = "already disposed") -> None:
		from ffl_integrations.fastbound import tasks
		from ffl_integrations.fastbound.client import FastBoundError
		client = MagicMock()
		client.post_acquisition.side_effect = FastBoundError(error, terminal=True)
		with patch.object(tasks, "fastbound_enabled", return_value=True), \
				patch.object(tasks.FastBoundClient, "from_settings", return_value=client):
			tasks.push_acquisition_to_fastbound(name, _retries=0, _commit=False)

	def test_a_terminal_push_rings_the_managers_once_per_reason(self):
		acq = _bare("FFL Acquisition", "NTFY-ACQ-1", docstatus=1)
		self._push(acq)
		self._push(acq)  # pushed again, refused the same way
		self.assertTrue(frappe.db.get_value("FFL Acquisition", acq, "fastbound_sync_error").startswith("[TERMINAL]"))
		self.assertRang("FFL Acquisition", acq, {STOCK: 1})
		self.assertIn("already disposed", _bell(STOCK, "FFL Acquisition", acq)[0])
		self._push(acq, error="contact has no license")  # fixed, then refused for a new reason
		self.assertRang("FFL Acquisition", acq, {STOCK: 2})


class DistributorShipments(_Base):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Distributor", "NTFY-DC"):
			frappe.get_doc({"doctype": "Distributor", "code": "NTFY-DC", "distributor_name": "NTFY DC",
				"provider": "rsr", "enabled": 0, "dropship_enabled": 0, "stocking_enabled": 0,
				"poll_enabled": 0}).insert(ignore_permissions=True)

	def _placed(self, order_type: str):
		do = frappe.get_doc({"doctype": "Distributor Order", "distributor": "NTFY-DC",
			"order_type": order_type, "account": "dealer"}).insert(ignore_permissions=True)
		frappe.db.set_value("Distributor Order", do.name, "status", "Placed", update_modified=False)
		do.reload()
		return do

	def _move(self, do, status):
		do.status = status
		do.save(ignore_permissions=True)

	def test_a_stocking_order_rings_on_part_and_full_shipment_once_each(self):
		do = self._placed("Stocking")
		self._move(do, "Partially Shipped")
		self._move(do, "Complete")
		do.save(ignore_permissions=True)  # a re-save in the same status
		self.assertRang("Distributor Order", do.name, {SALES: 2, STOCK: 2})
		self.assertEqual(_bell(STOCK, "Distributor Order", do.name),
			[f"NTFY-DC shipped part of {do.name}", f"NTFY-DC shipped {do.name}"])

	def test_an_order_closed_by_hand_after_receiving_does_not_ring(self):
		do = self._placed("Stocking")
		frappe.db.set_value("Distributor Order", do.name, "receive_status", "Received", update_modified=False)
		do.reload()
		self._move(do, "Complete")
		self.assertRang("Distributor Order", do.name, {})

	def test_a_drop_ship_does_not_ring_and_a_failed_bell_never_blocks_the_save(self):
		do = self._placed("Dropship Accessory")
		self._move(do, "Complete")
		self.assertRang("Distributor Order", do.name, {})

		do = self._placed("Procure To Store")
		with patch("ffl_core.alerts.notify_staff", side_effect=RuntimeError("boom")):
			self._move(do, "Complete")
		self.assertEqual(frappe.db.get_value("Distributor Order", do.name, "status"), "Complete")
		self.assertTrue(frappe.db.exists("Error Log", {"method": f"Shipment notification failed: {do.name}"}))
