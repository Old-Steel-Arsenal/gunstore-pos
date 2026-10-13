"""Sales Report, marketplace slice: the Texas worksheet and the
channel-fee margin columns.

Two numbers the store cannot produce by hand:

* **Marketplace sales (TX)** — Texas wants marketplace-provider sales inside
  Item 1 (Total Texas Sales) and back OUT of Item 2 (Taxable Sales), because
  GunBroker filed the tax under its own permit. Getting it from the sales-tax
  column is impossible: every GunBroker order shows $0 there by design.
* **Channel fees** — the final value fee is an expense, not COGS. Folding it
  into Cost would make the same gun look cheaper on the counter than on
  GunBroker; leaving it out entirely makes GunBroker look more profitable than
  it is. So Cost stays COGS and the fee gets its own column.

`ship_to_state` and `tax_exempt` come from two different places on purpose:
the state is stamped on the invoice at dispose time (it is what was shipped
against), while the exemption flag only ever existed on the GunBroker Order
and has to be read back through the link.
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.ffl_core.report.sales_report import sales_report as sr


def _hdr(name, *, voucher_type="Sales Invoice", date="2026-12-15", cust="C1",
         net=1100.0, total=1245.99, gb=None, woo=None, state=None,
         fvf=0.0, doc_fee=0.0):
	return {"name": name, "voucher_type": voucher_type, "posting_date": date,
		"customer": cust, "base_net_total": net, "grand_total": total,
		"is_return": 0, "is_ffl_transfer": 0, "po_no": None,
		"woo_online_order": woo, "gunbroker_order": gb,
		"gb_ship_to_state": state, "gb_final_value_fee": fvf,
		"gb_ffl_doc_fee": doc_fee, "exempt_from_sales_tax": 0,
		"company": "FFL Test"}


def _line(item="GUN-1", qty=1, net=1100.0, serial="SN1", name="row1"):
	return frappe._dict({"name": name, "item_code": item, "item_name": item,
		"qty": qty, "base_net_amount": net, "serial_no": serial,
		"consignment_commission_pct": None, "base_net_rate": net,
		"price_list_rate": net, "discount_amount": 0.0})


_META = {"GUN-1": {"is_firearm": 1, "item_group": "Rifle", "valuation_rate": 0.0}}


def _ship_row(parent, amount, name="t-ship"):
	return {"parent": parent, "name": name, "account_head": "Shipping Income - TSA",
		"base_tax_amount_after_discount_amount": amount, "charge_type": "Actual",
		"description": "Shipping"}


def _mpf_row(parent, amount, name="t-mpf"):
	return {"parent": parent, "name": name,
		"account_head": "GunBroker MPF Sales Tax Payable - TSA",
		"base_tax_amount_after_discount_amount": amount, "charge_type": "Actual",
		"description": "GunBroker MPF sales tax (GB Order #1)"}


def _msf_row(parent, amount, name="t-msf"):
	return {"parent": parent, "name": name,
		"account_head": "GunBroker Marketplace Fee Payable - TSA",
		"base_tax_amount_after_discount_amount": amount, "charge_type": "Actual",
		"description": "GunBroker marketplace service fee (GB Order #1)"}


_ACCT_CLASS = {
	"Shipping Income - TSA": sr._SHIPPING,
	"GunBroker MPF Sales Tax Payable - TSA": sr._MARKETPLACE_TAX,
	"GunBroker Marketplace Fee Payable - TSA": sr._MARKETPLACE_FEE,
}


def _product_rows(headers, taxes=None, exempt=None, detail=False):
	by_invoice: dict[str, list] = {}
	for t in (taxes or []):
		by_invoice.setdefault(t["parent"], []).append(t)
	return sr._build_product_rows(
		headers, {h["name"]: [_line(name=f"row-{h['name']}")] for h in headers},
		_META, {"SN1": 700.0}, {}, {}, by_invoice, _ACCT_CLASS, "", detail,
		marketplace_exempt=(exempt or {}))


def _total(rows):
	return next(r for r in rows if r.get("row_kind") == "total")


# ---------------------------------------------------------------- ship-to state

class TheShipToStateRidesEveryRowOfItsOrder(FrappeTestCase):
	"""The state has to be on the CSV lines, not just an order header: the
	worksheet is filtered and summed in a spreadsheet."""

	def test_a_gunbroker_order_stamps_its_state_on_the_product_row(self):
		rows = _product_rows([_hdr("SI-1", gb="GB-1", state="TX")])
		product = next(r for r in rows if r.get("row_kind") == "product")
		self.assertEqual(product["ship_to_state"], "TX")

	def test_a_counter_sale_has_no_ship_to_state(self):
		# POS Invoice has no such column; the report must read a blank, not raise.
		rows = _product_rows([_hdr("P-1", voucher_type="POS Invoice")])
		product = next(r for r in rows if r.get("row_kind") == "product")
		self.assertEqual(product["ship_to_state"], "")

	def test_a_web_order_has_no_ship_to_state(self):
		rows = _product_rows([_hdr("SI-2", woo="WOO-1")])
		product = next(r for r in rows if r.get("row_kind") == "product")
		self.assertEqual(product["ship_to_state"], "")

	def test_a_full_state_name_is_read_as_its_code(self):
		# reviewer major-1. Nothing normalises this value anywhere on the way
		# in: GunBroker's shipToState is str().strip()'d onto the order and
		# copied verbatim onto the invoice, and no sample of what GunBroker
		# actually sends exists in this run. The repo's own normaliser says
		# production addresses already mix "TX" and "Texas".
		rows = _product_rows([_hdr("SI-1", gb="GB-1", state="Texas")])
		product = next(r for r in rows if r.get("row_kind") == "product")
		self.assertEqual(product["ship_to_state"], "TX")

	def test_padding_and_case_are_read_as_the_code(self):
		rows = _product_rows([_hdr("SI-1", gb="GB-1", state=" tx ")])
		product = next(r for r in rows if r.get("row_kind") == "product")
		self.assertEqual(product["ship_to_state"], "TX")

	def test_an_unrecognisable_state_keeps_its_original_value(self):
		# Swallowing it would hide dirty data from the worksheet the state
		# wants kept for four years. Show it, do not count it.
		rows = _product_rows([_hdr("SI-1", gb="GB-1", state="Ontario")])
		product = next(r for r in rows if r.get("row_kind") == "product")
		self.assertEqual(product["ship_to_state"], "Ontario")

	def test_the_state_column_is_in_both_product_views(self):
		for detail in (False, True):
			fields = [c["fieldname"] for c in sr._product_columns(detail)]
			self.assertIn("ship_to_state", fields)


# ------------------------------------------------- Marketplace sales TX

class MarketplaceSalesTexas(FrappeTestCase):
	"""Item 1 of the Texas return wants goods + freight for every TX
	marketplace sale, and NOTHING the marketplace collected on top."""

	def _two_orders(self):
		# TX: goods 1100 + shipping 45; MPF 89.99 + MSF 11.00 are collected
		# THROUGH us and must not be in the figure.
		tx = _hdr("SI-TX", gb="GB-1", state="TX", net=1100.0, total=1245.99)
		out = _hdr("SI-OR", gb="GB-2", state="OR", net=800.0, total=830.0)
		taxes = [_ship_row("SI-TX", 45.0), _mpf_row("SI-TX", 89.99),
			_msf_row("SI-TX", 11.0), _ship_row("SI-OR", 30.0, name="t-ship2")]
		return [tx, out], taxes

	def test_the_total_row_carries_goods_plus_freight_for_texas_only(self):
		headers, taxes = self._two_orders()
		total = _total(_product_rows(headers, taxes))
		self.assertAlmostEqual(total["marketplace_tx"], 1145.0)   # 1100 + 45

	def test_the_marketplace_liabilities_are_excluded(self):
		# The negative control for the line above: if MPF/MSF leaked in the
		# figure would be 1245.99, which is exactly what the buyer paid.
		headers, taxes = self._two_orders()
		total = _total(_product_rows(headers, taxes))
		self.assertNotAlmostEqual(total["marketplace_tx"], 1245.99)

	def test_a_spelled_out_texas_still_reaches_the_card(self):
		# The failure this guards against is silent and one-directional: an
		# unrecognised spelling drops the sale out of Item 1, while Item 2 never
		# had it either (a GunBroker order is $0 sales tax by design). The sale
		# appears in NEITHER column of the return, and no flag goes red.
		tx = _hdr("SI-TX", gb="GB-1", state="Texas", net=1100.0)
		total = _total(_product_rows([tx], [_ship_row("SI-TX", 45.0)]))
		self.assertAlmostEqual(total["marketplace_tx"], 1145.0)

	def test_a_lowercase_padded_code_still_reaches_the_card(self):
		tx = _hdr("SI-TX", gb="GB-1", state=" tx ", net=1100.0)
		total = _total(_product_rows([tx], [_ship_row("SI-TX", 45.0)]))
		self.assertAlmostEqual(total["marketplace_tx"], 1145.0)

	def test_a_texas_sale_on_another_channel_is_not_marketplace_sales(self):
		# Only a marketplace provider's sale comes out of Item 2. A counter or
		# web sale to a Texan is ordinary taxable business.
		woo = _hdr("SI-W", woo="WOO-9", state="TX", net=500.0)
		total = _total(_product_rows([woo], [_ship_row("SI-W", 10.0)]))
		self.assertAlmostEqual(total["marketplace_tx"], 0.0)

	def test_the_card_shows_when_the_channel_filter_is_gunbroker(self):
		headers, taxes = self._two_orders()
		rows = _product_rows(headers, taxes)
		cards = sr._report_summary("Product", rows, {"channel": "GunBroker"})
		card = next(c for c in cards if c["label"] == "Marketplace sales (TX)")
		self.assertAlmostEqual(card["value"], 1145.0)

	def test_a_california_store_counts_its_own_state(self):
		"""The home state is the store's (interstate_tax.home_state), not Texas: on
		the California store the California sale is the marketplace line."""
		from unittest.mock import patch

		ca = _hdr("SI-CA", gb="GB-3", state="CA", net=900.0)
		tx = _hdr("SI-TX", gb="GB-1", state="TX", net=1100.0)
		with patch("ffl_core.compliance.interstate_tax.home_state", return_value="CA"):
			rows = _product_rows([ca, tx], [_ship_row("SI-CA", 20.0),
				_ship_row("SI-TX", 45.0, name="t-ship2")])
			cards = sr._report_summary("Product", rows, {"channel": "GunBroker"})
		self.assertAlmostEqual(_total(rows)["marketplace_tx"], 920.0)
		self.assertIn("Marketplace sales (CA)", [c["label"] for c in cards])

	def test_the_card_is_absent_on_an_unfiltered_run(self):
		# An all-channel total would read as "our Texas sales", which is the
		# opposite of what Item 1 wants from this line.
		headers, taxes = self._two_orders()
		rows = _product_rows(headers, taxes)
		labels = [c["label"] for c in sr._report_summary("Product", rows, {})]
		self.assertNotIn("Marketplace sales (TX)", labels)

	def test_the_card_is_absent_on_the_order_view(self):
		# The Order view has no shipping split, so it cannot produce the figure.
		labels = [c["label"] for c in sr._report_summary(
			"Order", [{"is_total": True, "net_sales": 1.0}], {"channel": "GunBroker"})]
		self.assertNotIn("Marketplace sales (TX)", labels)

	def test_summary_still_works_when_called_without_filters(self):
		# Backwards compatibility: the card is additive, the old 2-arg call must
		# keep working (every existing caller and test uses it).
		cards = sr._report_summary("Order", [{"is_total": True, "net_sales": 5.0}])
		self.assertEqual(cards[0]["value"], 5.0)


# ------------------------------------------------------------ exempt buyers

class ZeroTaxHasTwoDifferentCauses(FrappeTestCase):
	"""Both an exempt buyer and a no-sales-tax state produce a $0 MPF row. The
	worksheet has to say which, and the flag only exists on the GunBroker
	Order — the invoice never carried it."""

	def test_an_exempt_buyer_is_marked_on_the_row(self):
		rows = _product_rows([_hdr("SI-1", gb="GB-1", state="TX")],
			exempt={"GB-1": True})
		product = next(r for r in rows if r.get("row_kind") == "product")
		self.assertTrue(product["gb_tax_exempt"])

	def test_a_no_tax_state_order_is_not_marked(self):
		rows = _product_rows([_hdr("SI-2", gb="GB-2", state="OR")],
			exempt={"GB-2": False})
		product = next(r for r in rows if r.get("row_kind") == "product")
		self.assertFalse(product["gb_tax_exempt"])

	def test_the_exempt_flag_is_not_the_invoices_own_exemption(self):
		# exempt_from_sales_tax is the store's own certificate on file; a
		# marketplace exemption is the buyer's, held by GunBroker. Conflating
		# them would put a GB buyer's exemption on the store's Texas evidence.
		rows = _product_rows([_hdr("SI-3", gb="GB-3")], exempt={"GB-3": True})
		product = next(r for r in rows if r.get("row_kind") == "product")
		self.assertTrue(product["gb_tax_exempt"])
		self.assertFalse(product["exempt_from_sales_tax"])

	def test_the_column_is_in_the_flat_product_view(self):
		fields = [c["fieldname"] for c in sr._product_columns(False)]
		self.assertIn("gb_tax_exempt", fields)


# ------------------------------------------------------------------- channel fees

class ChannelFeesAreNotCost(FrappeTestCase):
	"""Cost is COGS. The marketplace's cut is an operating expense and gets a
	column of its own, so the same gun costs the same on every channel."""

	def _order_rows(self, headers):
		return sr._build_rows(headers,
			{h["name"]: [_line(name=f"row-{h['name']}")] for h in headers},
			_META, {"SN1": 700.0}, "")

	def test_the_fee_columns_are_the_two_gunbroker_fee_fields_added_up(self):
		rows = self._order_rows([_hdr("SI-1", gb="GB-1", fvf=52.0, doc_fee=5.0)])
		row = rows[0]
		self.assertAlmostEqual(row["channel_fees"], 57.0)

	def test_profit_after_fees_is_profit_less_the_fees(self):
		rows = self._order_rows([_hdr("SI-1", gb="GB-1", fvf=52.0, doc_fee=5.0)])
		row = rows[0]
		self.assertAlmostEqual(row["profit"], 400.0)               # 1100 − 700
		self.assertAlmostEqual(row["profit_after_fees"], 343.0)    # 400 − 57

	def test_cost_is_identical_on_web_and_gunbroker(self):
		# The acceptance check: the same gun, sold twice.
		web = self._order_rows([_hdr("SI-W", woo="WOO-1")])[0]
		gb = self._order_rows([_hdr("SI-G", gb="GB-1", fvf=52.0, doc_fee=5.0)])[0]
		self.assertAlmostEqual(web["cost"], gb["cost"])
		self.assertAlmostEqual(gb["cost"], 700.0)

	def test_a_non_marketplace_sale_has_zero_fees_and_equal_profits(self):
		row = self._order_rows([_hdr("SI-W", woo="WOO-1")])[0]
		self.assertAlmostEqual(row["channel_fees"], 0.0)
		self.assertAlmostEqual(row["profit_after_fees"], row["profit"])

	def test_the_order_total_row_sums_both(self):
		rows = self._order_rows([
			_hdr("SI-1", gb="GB-1", fvf=52.0, doc_fee=5.0),
			_hdr("SI-2", gb="GB-2", fvf=40.0, doc_fee=5.0, date="2026-12-16")])
		total = next(r for r in rows if r.get("is_total"))
		self.assertAlmostEqual(total["channel_fees"], 102.0)
		self.assertAlmostEqual(total["profit_after_fees"], total["profit"] - 102.0)

	def test_both_columns_are_in_the_order_view(self):
		fields = [c["fieldname"] for c in sr._columns()]
		self.assertIn("channel_fees", fields)
		self.assertIn("profit_after_fees", fields)

	def test_both_columns_are_in_the_order_detail_view(self):
		fields = [c["fieldname"] for c in sr._product_columns(True)]
		self.assertIn("channel_fees", fields)
		self.assertIn("profit_after_fees", fields)

	def test_the_product_order_row_carries_the_fees(self):
		rows = _product_rows([_hdr("SI-1", gb="GB-1", fvf=52.0, doc_fee=5.0)],
			detail=True)
		order = next(r for r in rows if r.get("row_kind") == "order")
		self.assertAlmostEqual(order["channel_fees"], 57.0)
		self.assertAlmostEqual(order["profit_after_fees"], order["profit"] - 57.0)

	def test_the_product_total_row_sums_the_fees(self):
		total = _total(_product_rows([
			_hdr("SI-1", gb="GB-1", fvf=52.0, doc_fee=5.0),
			_hdr("SI-2", gb="GB-2", fvf=40.0, doc_fee=5.0, date="2026-12-16")],
			detail=True))
		self.assertAlmostEqual(total["channel_fees"], 102.0)
		self.assertAlmostEqual(total["profit_after_fees"], total["profit"] - 102.0)
