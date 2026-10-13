"""Distributor invoice mirror: the RSR / SS normalisers against live-captured
shapes (2026-09-29, read-only probe on prod), and the sync's matching to
Distributor Orders on a real site (PO = order name, house order # = web_ref,
invoice # already on an order line, else outside POS)."""
from __future__ import annotations

import unittest
from unittest import mock

import frappe

from ffl_integrations.distributor import invoices, lanes
from ffl_integrations.distributor.rsr_provider import normalize_shipment
from ffl_integrations.distributor.ss_provider import _yymmdd, normalize_invoice
from ffl_integrations.tests.test_distributor_integration import _Base

# get-shipment, trimmed from the live capture (Items only; no PO, no totals).
RSR_SHIPMENT = {"Status": "OK", "StatusCode": "00", "Items": [
	{"ASNQty": "1", "Cost": "30.07", "UPCcode": "815835015408", "Invoiced": 1,
		"InvoiceQty": "1,", "Invoices": "4022784237,", "DateShipped": "20260916,",
		"TrackingNum": "543092672687,", "SerialNum": ",", "PartNum": "CMMG55CA601"},
	{"Cost": "$7.13", "Invoiced": 2, "InvoiceQty": "1,2,", "Invoices": "4022784236,4022784237,",
		"DateShipped": "20260915,20260916,", "TrackingNum": "111,222,", "SerialNum": "SN1,",
		"PartNum": "CMMG55CA9CB"},
	{"Cost": "5", "InvoiceQty": "3,", "Invoices": "4022780000,", "PartNum": "OTHER"},
	# Backordered: not on any invoice yet → on none of them.
	{"Cost": "9", "Invoiced": 0, "InvoiceQty": "", "Invoices": "", "PartNum": "BACKORDER"},
]}

# Split row whose FIRST invoice has no tracking / date yet: slots stay aligned.
RSR_SPLIT = {"Items": [{"Cost": "10", "Invoiced": 3, "Invoices": "A1,B2,", "InvoiceQty": "1,2,",
	"TrackingNum": ",222,", "DateShipped": ",20260917,", "PartNum": "SPLIT"}]}

SS_HEADER = {"S1CUST": "100001", "S1DATE": "2026-09-16T12:00:00+00:00", "S1INV": "23937563",
	"PONBR": "DO-YYMM-0002                  ", "INVGS": "80.11", "TAX": "0.00"}
SS_DETAIL = [
	{"SINV": "23937563", "SLIN": "1", "SINUM": "114289",
		"IDESC": "MAGPUL MAG556-MCT  PMAG 30 AR/M4 GEN M3 WINDOW 556", "SQTY": "5",
		"SPRC": "13.03", "SGROS": "68.58", "ORDNO": "12428127"},
	{"SINV": "23937563", "SLIN": "2", "SINUM": "64098", "IDESC": "*SHIPPING - FLAT RATE 2*",
		"SQTY": "1", "SPRC": "10.95", "SGROS": "11.53", "ORDNO": "12428127"},
]
SS_TRACKING = [{"INVNO": "23937563", "TRACKNO": "877287938009                  "}]


class TestNormalisers(unittest.TestCase):
	def test_rsr_shipment_bills_only_this_invoice_at_its_position(self):
		inv = normalize_shipment("4022784237", RSR_SHIPMENT, "dealer")
		self.assertEqual([(l["sku"], l["qty"], l["unit_cost"]) for l in inv["lines"]],
			[("CMMG55CA601", 1, 30.07), ("CMMG55CA9CB", 2, 7.13)])
		self.assertEqual(inv["total"], round(30.07 + 2 * 7.13, 2))
		self.assertEqual(inv["tracking_numbers"], ["543092672687", "222"])
		self.assertEqual(inv["invoice_date"], "2026-09-16")
		self.assertIsNone(inv["freight"])
		# Serials only pair to an invoice on a single-invoice row.
		self.assertEqual([l["serials"] for l in inv["lines"]], ["", ""])

	def test_rsr_serialized_units_on_one_invoice_are_summed(self):
		# Live 2026-09-29, invoice 4022814113: two NFA units, one slot each.
		resp = {"Items": [{"Cost": "785.00", "Invoiced": 2, "InvoiceQty": "1,1,",
			"Invoices": "4022814113,4022814113,", "DateShipped": "20260925,20260925,",
			"TrackingNum": "542839851575,542839851575,", "SerialNum": "UZ1021,UZ1024,",
			"PartNum": "Q-SIL-TB-QUICKIE-762", "UPCcode": "810152962107"}]}
		inv = normalize_shipment("4022814113", resp, "dealer")
		line = inv["lines"][0]
		self.assertEqual((line["qty"], line["amount"], inv["total"]), (2, 1570.0, 1570.0))
		self.assertEqual((line["serials"], line["tracking_number"]), ("UZ1021,UZ1024", "542839851575"))
		# Mixed row: serials follow this invoice's slots only.
		mixed = {"Items": [{"Cost": "10", "InvoiceQty": "1,1,1,", "Invoices": "A,B,A,",
			"SerialNum": "S1,S2,S3,", "PartNum": "P"}]}
		a = normalize_shipment("A", mixed, "dealer")["lines"][0]
		self.assertEqual((a["qty"], a["serials"]), (2, "S1,S3"))

	def test_rsr_split_row_reads_slots_by_position(self):
		a = normalize_shipment("A1", RSR_SPLIT, "dealer")
		b = normalize_shipment("B2", RSR_SPLIT, "dealer")
		self.assertEqual((a["lines"][0]["qty"], a["tracking_numbers"], a["invoice_date"]), (1, [], None))
		self.assertEqual((b["lines"][0]["qty"], b["tracking_numbers"], b["invoice_date"]),
			(2, ["222"], "2026-09-17"))

	def test_ss_invoice_splits_freight_and_strips_padding(self):
		inv = normalize_invoice(SS_HEADER, SS_DETAIL, SS_TRACKING)
		self.assertEqual(inv["po_number"], "DO-YYMM-0002")
		self.assertEqual(inv["order_ref"], "12428127")
		self.assertEqual(inv["invoice_date"], "2026-09-16")
		self.assertEqual((inv["total"], inv["freight"], inv["fees"]), (80.11, 11.53, 0))
		self.assertEqual([(l["sku"], l["qty"], l["amount"]) for l in inv["lines"]],
			[("114289", 5, 68.58)])
		self.assertEqual(inv["tracking_numbers"], ["877287938009"])

	def test_ss_invoice_keeps_every_order_it_bills(self):
		detail = SS_DETAIL + [{**SS_DETAIL[0], "SLIN": "3", "ORDNO": "12400000"},
			{"SINUM": "64100", "IDESC": "*PROCESSING FEE ORDERS LESS THAN $500", "SQTY": "1",
				"SPRC": "5", "SGROS": "5.00", "ORDNO": "0"}]
		inv = normalize_invoice(SS_HEADER, detail, [])
		self.assertEqual(inv["order_ref"], "12400000,12428127")
		# The fee line is a charge, not merchandise: summed into fees, off the lines.
		self.assertEqual(inv["fees"], 5.0)
		self.assertEqual([l["sku"] for l in inv["lines"]], ["114289", "114289"])

	def test_ss_only_orderless_star_lines_are_fees(self):
		fee = {"SINUM": "64100", "IDESC": "*PROCESSING FEE ORDERS LESS THAN $500", "SQTY": "1",
			"SPRC": "5", "SGROS": "5.00", "ORDNO": "0"}
		other = {"SINUM": "70000", "IDESC": "*HANDLING SURPRISE", "SQTY": "1", "SPRC": "3",
			"SGROS": "3.00", "ORDNO": "12428127"}
		inv = normalize_invoice(SS_HEADER, SS_DETAIL + [fee, other], [])
		self.assertEqual((inv["fees"], inv["freight"]), (5.0, 11.53))  # fee ORDNO 0; *SHIPPING = freight
		# An unknown "*" line with a real order stays a normal line, not a silent fee.
		self.assertEqual([l["sku"] for l in inv["lines"]], ["114289", "70000"])
		self.assertEqual(inv["order_ref"], "12428127")
		# ORDNO 0 never counts as freight either way.
		ship0 = {**SS_DETAIL[1], "ORDNO": "0"}
		inv = normalize_invoice(SS_HEADER, [SS_DETAIL[0], ship0], [])
		self.assertEqual((inv["freight"], inv["fees"]), (11.53, 0))

	def test_yymmdd(self):
		self.assertEqual((_yymmdd("260916"), _yymmdd(""), _yymmdd("2609")), ("2026-09-16", "", ""))


class _FakeProvider:
	"""Duck-types the two DistributorProvider invoice hooks."""

	def __init__(self, rows, open_orders=()):
		self.rows, self.open_orders, self.known_seen = rows, list(open_orders), None

	def list_invoices(self, since, known):
		self.known_seen = set(known)
		return [r for r in self.rows if r["invoice_number"] not in known]

	def list_open_orders(self):
		return self.open_orders

	def invoice_destination(self, row):
		return lanes.with_note(lanes.destination_of_account(row.get("account")))


def _inv(no, **kw):
	return {"invoice_number": no, "invoice_date": frappe.utils.nowdate(), "po_number": "",
		"order_ref": "", "total": 10, "tracking_numbers": ["T" + no],
		"lines": [{"sku": "DC-ACC", "qty": 1, "unit_cost": 10, "amount": 10}], **kw}


class _InvoiceRows(_Base):
	MERCH = {"sku": "1", "description": "MAGPUL PMAG", "qty": 1, "unit_cost": 80, "amount": 80}
	FEE = {"sku": "64100", "description": "*PROCESSING FEE ORDERS LESS THAN $500",
		"qty": 1, "unit_cost": 5, "amount": 5}
	SHIP = {"sku": "64098", "description": "*SHIPPING - FLAT RATE 2*", "qty": 1,
		"unit_cost": 10, "amount": 10}

	def setUp(self):
		if not frappe.db.exists("Distributor", "TEST-SS"):
			frappe.get_doc({"doctype": "Distributor", "code": "TEST-SS",
				"distributor_name": "Test SS", "provider": "ss", "enabled": 0,
				}).insert(ignore_permissions=True)
		frappe.db.delete("Distributor Invoice", {"distributor": ["in", ["TEST-DC", "TEST-SS"]]})

	def _make(self, distributor, no, order_ref, lines):
		return frappe.get_doc({"doctype": "Distributor Invoice", "distributor": distributor,
			"invoice_number": no, "order_ref": order_ref, "total": 90, "fees": 0,
			"lines": lines}).insert(ignore_permissions=True).name


class TestFeeBackfillPatch(_InvoiceRows):
	def test_patch_moves_ss_fee_lines_and_drops_zero_order_ref(self):
		from ffl_integrations.patches.split_sports_south_invoice_fees import execute
		old = self._make("TEST-SS", "PATCH-1", "0,123", [self.MERCH, self.FEE, self.SHIP])
		clean = self._make("TEST-SS", "PATCH-2", "456", [self.MERCH])
		for _ in range(2):  # idempotent: the second run changes nothing
			execute()
			got = frappe.get_doc("Distributor Invoice", old)
			self.assertEqual((got.order_ref, got.fees, [l.sku for l in got.lines]),
				("123", 5, ["1", "64098"]))  # the stored *SHIPPING-described line stays
		got = frappe.get_doc("Distributor Invoice", clean)
		self.assertEqual((got.order_ref, got.fees, len(got.lines)), ("456", 0, 1))

	def test_patch_leaves_other_providers_untouched(self):
		from ffl_integrations.patches.split_sports_south_invoice_fees import execute
		rsr = self._make("TEST-DC", "PATCH-3", "0,123", [self.MERCH, self.FEE])  # TEST-DC = rsr
		execute()
		got = frappe.get_doc("Distributor Invoice", rsr)
		self.assertEqual((got.order_ref, got.fees, len(got.lines)), ("0,123", 0, 2))


class TestSyncAndMatch(_Base):
	def setUp(self):
		frappe.db.delete("Distributor Invoice", {"distributor": "TEST-DC"})
		frappe.cache.delete_value(f"{invoices.OPEN_ORDERS_KEY}:TEST-DC")

	def _do(self, **fields):
		do = frappe.get_doc({"doctype": "Distributor Order", "distributor": "TEST-DC",
			"order_type": "Stocking", "account": "dealer",
			"lines": [{"sku": "DC-ACC", "item_code": "DC-ACC", "wish_qty": 1}]}).insert(
			ignore_permissions=True)
		if fields:
			frappe.db.set_value("Distributor Order", do.name, fields, update_modified=False)
		return do.name

	def test_matches_by_po_web_ref_and_line_invoice_else_outside(self):
		by_po = self._do()
		by_ref = self._do(web_ref="WR-777")
		by_line = self._do()
		frappe.db.set_value("Distributor Order Line", {"parent": by_line}, "invoices",
			"INV-A,INV-LINE", update_modified=False)
		fake = _FakeProvider([_inv("INV-PO", po_number=by_po),
			_inv("INV-REF", order_ref="WR-000,WR-777"),
			# Unsaveable (bad date): logged and skipped, the rest still land.
			_inv("INV-BAD", invoice_date="not-a-date"),
			_inv("INV-LINE"), _inv("INV-OUT")])
		with mock.patch.object(invoices, "get_providers", return_value={"rsr": fake}):
			invoices.sync_invoices()
			got = {r.invoice_number: r for r in frappe.get_all("Distributor Invoice",
				filters={"distributor": "TEST-DC"},
				fields=["invoice_number", "distributor_order", "outside_pos", "tracking_numbers"])}
			self.assertEqual(got["INV-PO"].distributor_order, by_po)
			self.assertEqual(got["INV-REF"].distributor_order, by_ref)
			self.assertEqual(got["INV-LINE"].distributor_order, by_line)
			self.assertEqual((got["INV-OUT"].distributor_order, got["INV-OUT"].outside_pos), (None, 1))
			self.assertEqual(got["INV-PO"].tracking_numbers, "TINV-PO")

			self.assertNotIn("INV-BAD", got)

			# Second run: nothing re-inserted, known numbers handed to the provider;
			# the bad one is not known, so it is offered again.
			invoices.sync_invoices()
			self.assertEqual(frappe.db.count("Distributor Invoice", {"distributor": "TEST-DC"}), 4)
			self.assertIn("INV-OUT", fake.known_seen)
			self.assertNotIn("INV-BAD", fake.known_seen)

			# The poll records INV-OUT on an order later → the next sync links it,
			# even with no invoice date (RSR can send none).
			frappe.db.set_value("Distributor Invoice", {"distributor": "TEST-DC",
				"invoice_number": "INV-OUT"}, "invoice_date", None, update_modified=False)
			late = self._do()
			frappe.db.set_value("Distributor Order Line", {"parent": late}, "invoices", "INV-OUT",
				update_modified=False)
			invoices.sync_invoices()
			self.assertEqual(frappe.db.get_value("Distributor Invoice",
				{"distributor": "TEST-DC", "invoice_number": "INV-OUT"},
				["distributor_order", "outside_pos"]), (late, 0))

		rows = invoices.list_invoices(distributor="TEST-DC")
		self.assertEqual(len(rows), 4)
		self.assertEqual(rows[0]["lines"][0]["sku"], "DC-ACC")

	def test_outside_open_orders_drops_pos_and_invoiced(self):
		mine = self._do()
		frappe.get_doc({"doctype": "Distributor Invoice", "distributor": "TEST-DC",
			"invoice_number": "INV-9", "order_ref": "ORD-OTHER,ORD-INVOICED", "outside_pos": 1}).insert(
			ignore_permissions=True)
		today = frappe.utils.nowdate()
		fake = _FakeProvider([], open_orders=[
			{"order_ref": "ORD-MINE", "po_number": mine, "order_date": today, "status": "D"},
			{"order_ref": "ORD-INVOICED", "po_number": "X", "order_date": today, "status": "D"},
			# Older than the invoice window: its invoice may predate the mirror.
			{"order_ref": "ORD-OLD", "po_number": "Y",
				"order_date": frappe.utils.add_days(today, -(invoices.LOOKBACK_DAYS + 5)), "status": "D"},
			{"order_ref": "ORD-WEB", "po_number": "WEBSITE", "order_date": today, "status": "O"},
		])
		# The page reads what the hourly sync cached — never the house live.
		with mock.patch.object(invoices, "get_providers", return_value={"rsr": fake}):
			invoices.sync_invoices()
		with mock.patch.object(fake, "list_open_orders", side_effect=AssertionError("live call")):
			rows = [r for r in invoices.outside_open_orders() if r["distributor"] == "TEST-DC"]
		self.assertEqual([r["order_ref"] for r in rows], ["ORD-WEB"])

	def test_pull_now_only_enqueues(self):
		with mock.patch.object(invoices, "enqueue_distributor") as enq, \
				mock.patch.object(invoices, "sync_invoices") as sync:
			self.assertEqual(invoices.sync_invoices_now(), {"queued": True})
		sync.assert_not_called()
		self.assertEqual(enq.call_args.kwargs["job_id"], invoices.SYNC_JOB_ID)
		self.assertTrue(enq.call_args.kwargs["deduplicate"])


class TestRSRListInvoices(unittest.TestCase):
	"""Only configured accounts are asked; one failing invoice is skipped."""

	def _run(self, dealer, fulfillment, client):
		from ffl_integrations.distributor.rsr_provider import RSRProvider

		settings = frappe._dict(api_enabled=1, dealer_account=dealer, fulfillment_account=fulfillment)
		with mock.patch("frappe.get_cached_doc", return_value=settings), \
				mock.patch("frappe.log_error") as log:
			return RSRProvider(client=client).list_invoices("2026-09-01", {"OLD"}), log

	def test_unconfigured_account_not_asked_and_bad_invoice_skipped(self):
		client = mock.Mock()
		client.get_shipments.return_value = ["OLD", None, "GOOD", "BAD"]
		client.get_shipment.side_effect = lambda no, acct: (
			(_ for _ in ()).throw(RuntimeError("boom")) if no == "BAD" else RSR_SHIPMENT)
		rows, log = self._run("D-1", "", client)
		client.get_shipments.assert_called_once_with("2026-09-01", "dealer")
		self.assertEqual([r["invoice_number"] for r in rows], ["GOOD"])
		self.assertEqual(log.call_count, 1)


class TestRefetchRSRPatch(_InvoiceRows):
	def test_patch_drops_refetchable_rsr_rows_only(self):
		from ffl_integrations.patches.refetch_rsr_invoices import execute
		today = frappe.utils.nowdate()
		recent = self._make("TEST-DC", "RSR-NEW", "", [self.MERCH])  # TEST-DC = rsr
		old = self._make("TEST-DC", "RSR-OLD", "", [self.MERCH])
		ss = self._make("TEST-SS", "SS-NEW", "", [self.MERCH])
		for name, days in ((recent, 0), (old, -(invoices.LOOKBACK_DAYS + 5)), (ss, 0)):
			frappe.db.set_value("Distributor Invoice", name, "invoice_date",
				frappe.utils.add_days(today, days), update_modified=False)
		execute()
		self.assertFalse(frappe.db.exists("Distributor Invoice", recent))
		self.assertFalse(frappe.db.exists("Distributor Invoice Line", {"parent": recent}))
		self.assertTrue(frappe.db.exists("Distributor Invoice", old))  # outside the window: kept
		self.assertTrue(frappe.db.exists("Distributor Invoice", ss))
		execute()  # a second run is harmless
