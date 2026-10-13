"""A non-firearm web sale moves no Bin until its stock leg posts (the Delivery
Note ``revenue.deliver_web_stock_lines`` books), so from checkout until that DN
the sold unit is in the Bin but not on the shelf. Every outbound stock writer
reads ``mapping.stock_for_item``, and until this change its Bin branch reported
the raw sum — so reconcile Pass 1 read WooCommerce's own checkout decrement as
drift and "healed" the sold unit back onto the storefront. The
one-of-a-kind IJN M1883 dirk sold at 21:16 and was resurrected to instock by
the 22:00 sweep, 2026-08-28.

``mapping.pending_web_line_qty`` closes the window with three legs, one per
pipeline stage — Pending Fulfillment lines, Transferred-without-submitted-SI
lines, and submitted-SI rows not yet covered by submitted non-return DNs — the
last two floored at the audit backstop's lookback so an ancient stuck document
cannot delist a restocked item for ever. These tests pin each leg, the floor,
and the branch routing that keeps firearms out of it.

Firearms are out of scope on purpose: their availability is already spent
through serial reservations and unassigned claims (`test_woo_quantity_hold`),
and `stock_for_item` routes every firearm away from the Bin branch — the only
caller of the new subtraction (the routing itself is pinned below).
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

MAPPING = "ffl_woo_sync.woocommerce.mapping"
ACCESSORY = "QA-WLC-DIRK"
OTHER = "QA-WLC-OTHER"
WAREHOUSE = "Stores - FT"


def _ensure_items(*specs):
	"""Item masters created INSIDE the test transaction (the harness rollback
	takes them), on the REAL `Accessory` leaf group — an arbitrary `is_group=0`
	pick can be a firearm group on another site, and these tests read the real
	Item row: no wholesale `frappe.db.get_value` patch, which would also feed
	the distributor hook's failure path (`notify_admins` writes Redis throttle
	keys no rollback can undo — the hazard ffl_core/custody.py documents)."""
	from ffl_core.setup import item_groups

	item_groups.ensure()
	for spec in specs or ({"item_code": ACCESSORY}, {"item_code": OTHER}):
		code = spec["item_code"]
		if frappe.db.exists("Item", code):
			continue
		doc = {"doctype": "Item", "item_code": code, "item_name": code,
			"item_group": "Accessory", "stock_uom": "Nos", "is_stock_item": 1}
		doc.update(spec)
		d = frappe.get_doc(doc)
		d.flags.ignore_permissions = True
		d.insert()


def _make_web_order(*, lines, order_status="Pending Fulfillment", site="retail",
		transferred_days_ago=None):
	"""A real Woo Online Order, so the commitment query runs against the database."""
	doc = frappe.get_doc({
		"doctype": "Woo Online Order",
		"woo_order_id": f"WLC-{frappe.generate_hash(length=8)}",
		"order_status": order_status, "site": site, "customer_name": "QA Commit",
		"lines": [{
			"item_code": row.get("item_code", ACCESSORY),
			"woo_line_id": str(i + 1),
			"qty": row.get("qty", 1),
			"unit_price": 629, "subtotal": 629, "line_total": 629, "line_tax": 0,
		} for i, row in enumerate(lines)],
	})
	doc.flags.ignore_permissions = True
	doc.insert()
	if transferred_days_ago is not None:
		frappe.db.set_value("Woo Online Order", doc.name, "transferred_at",
			add_to_date(now_datetime(), days=-transferred_days_ago),
			update_modified=False)
	return doc


def _make_si(order_name, rows=(), *, days_ago=1):
	"""A raw submitted web Sales Invoice (db_insert, no validate/submit chain —
	the test_order_audit fixture pattern). rows: (item_code, qty, warehouse,
	delivered_by_supplier). Leg 3 never joins the order table, so `order_name`
	may name an order that does not exist."""
	name = f"QA-WLC-SI-{frappe.generate_hash(length=8)}"
	when = add_to_date(now_datetime(), days=-days_ago)
	frappe.get_doc({
		"doctype": "Sales Invoice", "name": name, "docstatus": 1,
		"customer": "_Test Customer", "woo_online_order": order_name,
		"update_stock": 0, "posting_date": when.date(), "posting_time": when.time(),
		"set_posting_time": 1, "creation": when, "modified": when,
		"owner": "Administrator", "modified_by": "Administrator",
	}).db_insert()
	out = []
	for idx, (item, qty, warehouse, dropship) in enumerate(rows, start=1):
		rname = f"{name}-r{idx}"
		frappe.get_doc({
			"doctype": "Sales Invoice Item", "name": rname, "parent": name,
			"parenttype": "Sales Invoice", "parentfield": "items", "idx": idx,
			"docstatus": 1, "item_code": item, "qty": qty,
			"warehouse": warehouse, "delivered_by_supplier": dropship,
			"creation": when, "modified": when,
			"owner": "Administrator", "modified_by": "Administrator",
		}).db_insert()
		out.append(rname)
	return name, out


def _make_dn_row(si_detail, qty, *, docstatus=1, is_return=0):
	dn = f"QA-WLC-DN-{frappe.generate_hash(length=8)}"
	now = now_datetime()
	frappe.get_doc({
		"doctype": "Delivery Note", "name": dn, "docstatus": docstatus,
		"is_return": is_return, "customer": "_Test Customer",
		"posting_date": now.date(), "creation": now, "modified": now,
		"owner": "Administrator", "modified_by": "Administrator",
	}).db_insert()
	frappe.get_doc({
		"doctype": "Delivery Note Item", "name": f"{dn}-row", "parent": dn,
		"parenttype": "Delivery Note", "parentfield": "items", "idx": 1,
		"docstatus": docstatus, "item_code": ACCESSORY, "qty": qty,
		"si_detail": si_detail, "creation": now, "modified": now,
		"owner": "Administrator", "modified_by": "Administrator",
	}).db_insert()


# ---------------------------------------------------------------------------
# pending_web_line_qty — against a REAL database
# ---------------------------------------------------------------------------

class TestPendingWebLineQty(FrappeTestCase):
	"""This count is subtracted straight from what WooCommerce is told is on the
	shelf. Asserting on its SQL text would pass just as happily for `WHERE 1=1`,
	so every case goes against real rows (the sibling suite's rule). Deltas, not
	absolutes: FrappeTestCase rolls back per CLASS, so sibling tests' rows are
	visible here."""

	def setUp(self):
		super().setUp()
		_ensure_items()

	def _qty(self, code=ACCESSORY):
		from ffl_woo_sync.woocommerce.mapping import pending_web_line_qty
		return pending_web_line_qty(code)

	# -- leg 1: Pending Fulfillment lines --

	def test_a_pending_order_line_commits_its_qty(self):
		before = self._qty()
		_make_web_order(lines=[{}])
		self.assertEqual(self._qty(), before + 1)

	def test_qty_is_summed_not_counted(self):
		"""Accessory lines sell in multiples — a qty-3 line owes three units,
		and a second order's units stack on top."""
		before = self._qty()
		_make_web_order(lines=[{"qty": 3}])
		_make_web_order(lines=[{"qty": 2}])
		self.assertEqual(self._qty(), before + 5)

	def test_a_cancelled_order_commits_nothing(self):
		before = self._qty()
		_make_web_order(lines=[{}], order_status="Cancelled")
		self.assertEqual(self._qty(), before)

	def test_another_items_lines_do_not_count(self):
		before = self._qty()
		_make_web_order(lines=[{"item_code": OTHER}])
		self.assertEqual(self._qty(), before)

	# -- leg 2: Transferred, no submitted SI --

	def test_a_transferred_order_without_invoice_still_commits(self):
		"""Transferred flips BEFORE the SI/DN book (the completion job may be in
		flight, crashed, or parked on draft_total_mismatch) — the Bin has not
		moved, so the unit is still spoken for. This is the branch a review
		flagged: without it, reconcile resurrects the sold unit exactly as in
		the original incident, just one state later."""
		before = self._qty()
		_make_web_order(lines=[{}], order_status="Transferred")
		self.assertEqual(self._qty(), before + 1)

	def test_a_transferred_order_with_a_submitted_invoice_leaves_leg_two(self):
		"""Once the SI is submitted the SI rows (leg 3) own the count — the
		order lines must stop counting or every invoiced order double-commits."""
		before = self._qty()
		order = _make_web_order(lines=[{}], order_status="Transferred")
		_make_si(order.name, rows=())  # header only: leg 3 contributes nothing
		self.assertEqual(self._qty(), before)

	def test_a_stale_transferred_order_stops_committing(self):
		"""Past the audit backstop's lookback the stuck document belongs to the
		humans its alerts paged — an ancient unresolved order must not delist a
		restocked item for ever (nor mass-delist on first install over history
		that predates the revenue leg)."""
		before = self._qty()
		_make_web_order(lines=[{}], order_status="Transferred",
			transferred_days_ago=31)
		self.assertEqual(self._qty(), before)

	# -- leg 3: submitted SI rows not yet delivered --

	def test_an_invoiced_undelivered_row_still_commits(self):
		before = self._qty()
		_make_si("QA-WLC-GHOST-ORDER", rows=[(ACCESSORY, 2, WAREHOUSE, 0)])
		self.assertEqual(self._qty(), before + 2)

	def test_a_fully_delivered_row_commits_nothing(self):
		before = self._qty()
		_si, rows = _make_si("QA-WLC-GHOST-ORDER", rows=[(ACCESSORY, 2, WAREHOUSE, 0)])
		_make_dn_row(rows[0], 2)
		self.assertEqual(self._qty(), before)

	def test_a_partially_delivered_row_commits_the_remainder(self):
		before = self._qty()
		_si, rows = _make_si("QA-WLC-GHOST-ORDER", rows=[(ACCESSORY, 3, WAREHOUSE, 0)])
		_make_dn_row(rows[0], 1)
		self.assertEqual(self._qty(), before + 2)

	def test_a_return_dn_leaves_the_row_owed(self):
		"""Same rule as order_audit._invoiced_without_delivery: a return means
		the goods came back under a human's hands — suppress, don't advertise."""
		before = self._qty()
		_si, rows = _make_si("QA-WLC-GHOST-ORDER", rows=[(ACCESSORY, 1, WAREHOUSE, 0)])
		_make_dn_row(rows[0], 1, is_return=1)
		self.assertEqual(self._qty(), before + 1)

	def test_a_dropshipped_si_row_commits_nothing(self):
		"""Leg 3 is lane-aware: a supplier-shipped unit never held our Bin."""
		before = self._qty()
		_make_si("QA-WLC-GHOST-ORDER", rows=[(ACCESSORY, 1, "", 1)])
		self.assertEqual(self._qty(), before)

	def test_an_old_invoice_stops_committing(self):
		before = self._qty()
		_make_si("QA-WLC-GHOST-ORDER", rows=[(ACCESSORY, 1, WAREHOUSE, 0)],
			days_ago=31)
		self.assertEqual(self._qty(), before)


# ---------------------------------------------------------------------------
# stock_for_item — the Bin branch nets committed units off
# ---------------------------------------------------------------------------

class TestStockForItemWebCommit(FrappeTestCase):
	"""The reconcile ground truth the dirk incident was about: Pass 1, the
	stock push and the SLE doc_event all read this number.

	One FRESH item per test, not the module fixtures: FrappeTestCase rolls the
	transaction back per CLASS, so a sibling test's order rows survive into this
	one — an absolute count needs a code nothing else has touched. Only
	`get_item_stock` is patched; the Item row, custody and the distributor hook
	all run real (see _ensure_items on why no `frappe.db.get_value` patch)."""

	def _item(self, **spec):
		code = f"QA-WLC-{frappe.generate_hash(length=8)}"
		spec["item_code"] = code
		_ensure_items(spec)
		return code

	def _stock(self, code, bin_qty):
		from ffl_woo_sync.woocommerce import mapping
		with patch(f"{MAPPING}.get_item_stock", return_value={code: float(bin_qty)}):
			return mapping.stock_for_item(code)

	def test_committed_units_come_off_the_bin(self):
		code = self._item()
		_make_web_order(lines=[{"item_code": code}])
		self.assertEqual(self._stock(code, 5), 4)

	def test_the_dirk_case_a_sold_unit_stays_off_the_storefront(self):
		"""Bin 1, one order awaiting fulfillment → the storefront must see 0, or
		reconcile Pass 1 reads Woo's own checkout decrement as drift and
		resurrects the sold unit."""
		code = self._item()
		_make_web_order(lines=[{"item_code": code}])
		self.assertEqual(self._stock(code, 1), 0)

	def test_oversold_clamps_to_zero(self):
		"""Clamped at the funnel like every other branch: a negative would be
		stored by WooCommerce as 0 and read by every later sweep as drift,
		re-pushing for ever. (The oversold warning is logged, not raised.)"""
		code = self._item()
		_make_web_order(lines=[{"item_code": code, "qty": 2}])
		self.assertEqual(self._stock(code, 1), 0)

	def test_no_commitment_leaves_the_bin_count_alone(self):
		self.assertEqual(self._stock(self._item(), 5), 5)

	def test_a_uniform_price_firearm_never_consults_line_commitments(self):
		"""Pins the branch order the whole no-double-count argument rests on: a
		uniform-price firearm spends its availability through serial claims
		(`unassigned_web_claims`), and its order LINES name the same guns — if
		the Bin branch's subtraction ever ran for it, every web-sold rifle would
		come off the count twice. A reorder of stock_for_item's branches would
		fail here, not silently in production."""
		code = self._item(is_firearm=1, has_serial_no=1, uniform_sell_price=1)
		_make_web_order(lines=[{"item_code": code}])  # a line that WOULD count
		from ffl_woo_sync.woocommerce import mapping
		with patch(f"{MAPPING}.pending_web_line_qty") as spy:
			mapping.stock_for_item(code)
		spy.assert_not_called()
