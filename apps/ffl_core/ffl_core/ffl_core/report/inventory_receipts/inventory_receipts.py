"""Inventory Receipts — everything that ENTERED stock in a date range, by
category (Firearm / Ammunition / Accessory / Other) and by how it came in.

Stock arrives through four kinds of voucher and no ERPNext report puts them on
one page: Purchase Receipts (Receive Goods — suppliers, private sellers,
consignors), Stock Entry Material Receipts (the used-gun Intake page, FastBound
backfill, manual add_stock), customer returns (Sales / POS Invoice with +qty)
and Stock Reconciliation gains. The accountant needs "what came in, from whom,
at what cost" for a period; ERPNext's Stock Ledger is per-row, Stock-role-only
and mixes internal transfers in, and Item-wise Purchase Register is built on
Purchase Invoices the store never records.

Source of truth = Stock Ledger Entry with actual_qty > 0 and is_cancelled = 0.
Every inbound path ends there, so nothing can be missed, and Cost is the
stock_value_difference the GL carries — the Summary ties to Stock In Hand.

Receipt classes (derived per voucher, see _classify):
  Purchase     Purchase Receipt from a supplier / private seller
  Trade-in     receipt whose source is a trade-in (Intake source_type or a
               Receive Goods source containing "trade")
  Consignment  PR with a consignment acquisition type ($0 cost, no AP)
  Intake       Stock Entry · Material Receipt (Intake page / backfill / manual)
  Return       customer return — SI / POS Invoice / Delivery Note with +qty
  Adjustment   Stock Reconciliation count gain (units that were not on the books)
  Revaluation  Stock Reconciliation that only changed a unit's cost — a
               serialized cost correction posts the same serial out and back in
               within one voucher; a plain item's row has qty delta 0. Shown
               with 0 units and the cost DIFFERENCE, so Cost Received still
               moves with Stock In Hand while the unit counts stay honest
  Transfer     Material Transfer INTO a warehouse — an internal move, not new
               inventory; hidden unless include_transfers
  Custody      into a customer-custody warehouse (FFL transfer for a customer —
               customer property, cost 0); hidden unless include_custody
  Other        anything else (Purchase Invoice with update_stock, Repack …)

Views: Receipts (tree: voucher → lines → serial units, the default),
Summary (category → class roll-up), Units (flat, one row per unit — the CSV /
MCP grain). Every view returns the same summary cards.

The books of record are QuickBooks (purchases are
prepaid, so there is no payable to track). ERPNext's Purchase Invoice / Stock
Received But Not Billed tail is therefore NOT surfaced here — the accountant
books what this report shows, she does not reconcile it against ERPNext's GL.
"""
from __future__ import annotations

from collections import OrderedDict

import frappe
from frappe import _
from frappe.utils import flt, getdate

from ffl_core import report_cards
from ffl_core.acquisition import TRADE_IN_SOURCE
from ffl_core.cash_drawer import unsettled_seller_receipts
from ffl_core.firearm import _is_firearm_row, is_consignment_acquisition_type
from ffl_core.warehouses import custody_warehouse_names

FIREARM, AMMUNITION, ACCESSORY, OTHER_CATEGORY = "Firearm", "Ammunition", "Accessory", "Other"
CATEGORIES = (FIREARM, AMMUNITION, ACCESSORY, OTHER_CATEGORY)
# Top-level Item Groups that roll up to a report category. "Used Firearm" is a
# top-level leaf in setup/item_groups.py (a legacy home for intake guns), so it
# is named here explicitly; every other firearm group sits under "Firearm".
_TOP_GROUP_CATEGORY = {
	"Firearm": FIREARM, "Used Firearm": FIREARM,
	"Ammunition": AMMUNITION, "Accessory": ACCESSORY,
	# Gun parts (2026-09-26) keep the accessory column they were counted in before
	# Handguards & Rails / Stocks & Grips moved out of Accessory — no change of basis.
	"Parts": ACCESSORY,
}

PURCHASE, TRADE_IN, CONSIGNMENT, INTAKE, RETURN, ADJUSTMENT, REVALUATION, TRANSFER, CUSTODY, OTHER = (
	"Purchase", "Trade-in", "Consignment", "Intake", "Return", "Adjustment", "Revaluation",
	"Transfer", "Custody", "Other")
CLASSES = (PURCHASE, TRADE_IN, CONSIGNMENT, INTAKE, RETURN, ADJUSTMENT, REVALUATION, TRANSFER, CUSTODY, OTHER)
# Hidden unless the matching filter is on — or the class is selected outright.
_OPT_IN = {TRANSFER: "include_transfers", CUSTODY: "include_custody"}

VIEWS = ("Receipts", "Summary", "Units")

_TRANSFER_PURPOSES = {"Material Transfer", "Material Transfer for Manufacture", "Send to Subcontractor"}
_RETURN_VOUCHERS = {"Sales Invoice", "POS Invoice", "Delivery Note"}
# FastBound backfill books 0.01 when the bound book carries no cost
# (importer._create_stock_receipt) — that is a missing cost, not a penny gun.
_ZERO_COST_CEILING = 0.01

# Flag labels — human-readable so the CSV export needs no legend; the .js maps
# them to symbols on screen.
FLAG_NO_COST, FLAG_NO_AD = "No cost", "No A&D"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	_validate(filters)
	units = _fetch_units(filters)
	view = filters.view or "Receipts"
	if view == "Summary":
		columns, data = _summary_view(units)
	elif view == "Units":
		columns, data = _units_view(units)
	else:
		columns, data = _receipts_view(units)
	unsettled = unsettled_seller_receipts(filters.from_date, filters.to_date)
	return columns, data, _message(view, data, filters, unsettled), None, _cards(units, unsettled)


# ------------------------------------------------------------------ validation

def _validate(filters) -> None:
	if not filters.get("from_date") or not filters.get("to_date"):
		frappe.throw(_("From Date and To Date are required"))
	if getdate(filters.from_date) > getdate(filters.to_date):
		frappe.throw(_("From Date must be on or before To Date"))
	# Unknown segment values are REFUSED, not ignored — an unfiltered total must
	# never be mistaken for a filtered one (same stance as Sales Report).
	for key, allowed in (("view", VIEWS), ("category", CATEGORIES), ("receipt_class", CLASSES)):
		value = (str(filters.get(key) or "")).strip()
		filters[key] = value
		if value and value not in allowed:
			frappe.throw(_("Unknown {0} {1} — use one of: {2} (or leave it empty).").format(
				key, value, ", ".join(allowed)))


# ------------------------------------------------------------------ data fetch

def _fetch_units(filters) -> list[dict]:
	"""One dict per UNIT received: a serial-tracked item explodes into one unit
	per serial (each with its own cost from the Serial and Batch Bundle); any
	other item is one unit-row per ledger line carrying the line qty. Already
	classified, sourced, flagged and filtered."""
	sles = frappe.db.sql("""
		SELECT sle.name AS sle, sle.posting_date, sle.posting_time, sle.creation,
			sle.voucher_type, sle.voucher_no, sle.voucher_detail_no,
			sle.item_code, sle.warehouse, sle.actual_qty, sle.incoming_rate,
			sle.stock_value_difference, sle.serial_and_batch_bundle, sle.serial_no,
			i.item_name, i.item_group, i.has_serial_no, i.is_firearm
		FROM `tabStock Ledger Entry` sle
		JOIN `tabItem` i ON i.name = sle.item_code
		WHERE sle.is_cancelled = 0 AND sle.docstatus < 2
			AND (sle.actual_qty > 0 OR sle.voucher_type = 'Stock Reconciliation')
			AND sle.posting_date BETWEEN %(from_date)s AND %(to_date)s
		ORDER BY sle.posting_date, sle.posting_time, sle.voucher_no, sle.creation, sle.name
	""", {"from_date": filters.from_date, "to_date": filters.to_date}, as_dict=True)
	if not sles:
		return []
	bundle_rows = _fetch_bundle_rows(sles)
	sles, revalued = _resolve_reconciliations(sles, bundle_rows)
	if not sles:
		return []

	category_of = _category_map()
	custody = custody_warehouse_names()
	headers = _fetch_headers(sles)
	transfer_from = _fetch_transfer_sources(sles)
	acquisitions = _fetch_acquisitions(sles, bundle_rows)

	units: list[dict] = []
	for sle in sles:
		header = headers.get((sle.voucher_type, sle.voucher_no)) or frappe._dict()
		category = FIREARM if _is_firearm_row(sle) else category_of.get(sle.item_group, OTHER_CATEGORY)
		exploded = _explode(sle, bundle_rows)
		for u in exploded:
			u["acquisition"] = _pick_acquisition(acquisitions.get(u["serial_no"]), sle.posting_date) \
				if u["serial_no"] else None
			_apply_revaluation(u, sle, revalued)
		first_acq = next((u["acquisition"] for u in exploded if u["acquisition"]), None)
		receipt_class = _classify(sle, header, custody, first_acq)
		source = _source(sle, header, receipt_class, first_acq,
			transfer_from.get(sle.voucher_detail_no))
		for u in exploded:
			cls = REVALUATION if u.get("revaluation") else receipt_class
			u.update({
				"posting_date": sle.posting_date, "voucher_type": sle.voucher_type,
				"voucher_no": sle.voucher_no, "sle": sle.sle, "warehouse": sle.warehouse,
				"item_code": sle.item_code, "item_name": sle.item_name,
				"item_group": sle.item_group, "category": category,
				"receipt_class": cls, "source": _("Cost correction") if cls == REVALUATION else source,
				"supplier": header.get("supplier"),
				"acquisition": u["acquisition"]["name"] if u["acquisition"] else None,
			})
			u["flags"] = _flags(u, category, cls)
			units.append(u)
	return [u for u in units if _passes(u, filters)]


def _resolve_reconciliations(sles, bundle_rows):
	"""Make Stock Reconciliation rows say what they mean.

	Serialized: ERPNext posts a cost correction as the SAME serial going out
	(−1, old bundle) and back in (+1, new bundle) inside one voucher. The −1 rows
	are never receipts — they are folded into `revalued[(voucher, serial)]` (their
	negative stock_value_difference) and dropped; the matching +1 unit later
	becomes a Revaluation with 0 units and the net cost change (_apply_revaluation).
	A +1 with no −1 partner is a genuine count gain and stays an Adjustment.

	Plain items: the ledger row carries actual_qty 0 and only qty_after_transaction;
	the delta lives on the Stock Reconciliation Item. delta > 0 → a receipt of that
	many units; delta 0 with a value change → Revaluation; delta < 0 → not inbound.

	Returns (rows to emit, revalued)."""
	sr = [x for x in sles if x.voucher_type == "Stock Reconciliation"]
	if not sr:
		return sles, {}
	revalued: dict[tuple, float] = {}
	for x in sr:
		if flt(x.actual_qty) < 0:
			for u in _explode(x, bundle_rows):
				if u["serial_no"]:
					key = (x.voucher_no, u["serial_no"])
					revalued[key] = revalued.get(key, 0.0) + flt(u["cost"])
	plain = [x for x in sr if not flt(x.actual_qty)]
	deltas = dict(frappe.get_all("Stock Reconciliation Item",
		filters={"name": ("in", [x.voucher_detail_no for x in plain if x.voucher_detail_no])},
		fields=["name", "quantity_difference"], as_list=True)) if plain else {}
	out = []
	for x in sles:
		if x.voucher_type != "Stock Reconciliation":
			out.append(x)
			continue
		qty = flt(x.actual_qty)
		if qty < 0:
			continue  # folded into `revalued`
		if qty == 0:
			delta = flt(deltas.get(x.voucher_detail_no))
			if delta > 0:
				x.actual_qty = delta
				x.incoming_rate = flt(x.stock_value_difference) / delta
			elif delta == 0 and flt(x.stock_value_difference):
				x.revaluation_only = True
			else:
				continue
		out.append(x)
	return out, revalued


def _apply_revaluation(unit, sle, revalued) -> None:
	"""Turn a Stock Reconciliation re-entry into a Revaluation unit in place:
	0 units, cost = what the correction added to (or took from) Stock In Hand."""
	if sle.voucher_type != "Stock Reconciliation":
		return
	key = (sle.voucher_no, unit["serial_no"])
	if unit["serial_no"] and key in revalued:
		unit.update({"qty": 0.0, "rate": None, "cost": flt(unit["cost"]) + revalued[key], "revaluation": True})
	elif sle.get("revaluation_only"):
		unit.update({"qty": 0.0, "rate": None, "cost": flt(sle.stock_value_difference), "revaluation": True})


def _category_map() -> dict[str, str]:
	"""item_group → report category, via the Item Group nested set: a leaf rolls
	up to its top-level ancestor (the child of the root), which maps through
	_TOP_GROUP_CATEGORY; anything else — Service, ERPNext's stock groups — is
	Other. Legacy leaves (Handgun) roll up with their parent, so a rename never
	has to touch this."""
	groups = frappe.get_all("Item Group", fields=["name", "parent_item_group", "lft", "rgt"])
	roots = {g.name for g in groups if not g.parent_item_group}
	tops = [g for g in groups if g.parent_item_group in roots]
	out = {}
	for g in groups:
		top = next((t for t in tops if t.lft <= g.lft and g.rgt <= t.rgt), None)
		out[g.name] = _TOP_GROUP_CATEGORY.get(top.name if top else g.name, OTHER_CATEGORY)
	return out


def _fetch_headers(sles) -> dict:
	"""Voucher header facts the classification / source columns need, one
	get_all per voucher type. Keyed (voucher_type, voucher_no)."""
	names: dict[str, set] = {}
	for s in sles:
		names.setdefault(s.voucher_type, set()).add(s.voucher_no)
	fields = {
		"Purchase Receipt": ["name", "supplier", "supplier_name", "ffl_source",
			"ffl_acquisition_type", "ffl_seller_customer", "ffl_transfer_for_customer"],
		"Stock Entry": ["name", "purpose", "stock_entry_type", "remarks", "from_warehouse"],
		"Stock Reconciliation": ["name", "purpose"],
		"Purchase Invoice": ["name", "supplier", "supplier_name"],
	}
	for vt in _RETURN_VOUCHERS:
		fields[vt] = ["name", "customer", "customer_name", "return_against"]
	out = {}
	for vt, vnames in names.items():
		if vt not in fields:
			continue
		for row in frappe.get_all(vt, filters={"name": ("in", list(vnames))}, fields=fields[vt]):
			out[(vt, row.name)] = row
	# The real seller of an individual purchase is a Customer link; the Supplier
	# on the PR is ERPNext's mandatory placeholder (receive_goods._ensure_supplier).
	customers = {r.get("ffl_seller_customer") or r.get("ffl_transfer_for_customer")
		for (vt, _n), r in out.items() if vt == "Purchase Receipt"} - {None, ""}
	if customers:
		cust_names = dict(frappe.get_all("Customer", filters={"name": ("in", list(customers))},
			fields=["name", "customer_name"], as_list=True))
		for (vt, _n), r in out.items():
			if vt == "Purchase Receipt":
				r["seller_name"] = cust_names.get(r.get("ffl_seller_customer"))
				r["transfer_customer_name"] = cust_names.get(r.get("ffl_transfer_for_customer"))
	return out


def _fetch_transfer_sources(sles) -> dict[str, str]:
	"""voucher_detail_no → the warehouse a Stock Entry line came FROM. The header's
	from_warehouse is only a default; the row is where the fact lives."""
	details = [s.voucher_detail_no for s in sles if s.voucher_type == "Stock Entry" and s.voucher_detail_no]
	if not details:
		return {}
	return dict(frappe.get_all("Stock Entry Detail", filters={"name": ("in", details)},
		fields=["name", "s_warehouse"], as_list=True))


def _fetch_bundle_rows(sles) -> dict[str, list]:
	bundles = [s.serial_and_batch_bundle for s in sles if s.serial_and_batch_bundle]
	if not bundles:
		return {}
	out: dict[str, list] = {}
	for r in frappe.get_all("Serial and Batch Entry", filters={"parent": ("in", bundles)},
			fields=["parent", "serial_no", "qty", "incoming_rate", "stock_value_difference"],
			order_by="idx asc"):
		out.setdefault(r.parent, []).append(r)
	return out


def _explode(sle, bundle_rows) -> list[dict]:
	"""A ledger line → its units. Bundle path (v16 default): one unit per Serial
	and Batch Entry, each with the cost ERPNext booked for THAT serial. Legacy
	path (use_serial_batch_fields): the newline list on the SLE, cost split
	evenly. Anything without serials is one unit-row carrying the line qty."""
	rows = bundle_rows.get(sle.serial_and_batch_bundle) if sle.serial_and_batch_bundle else None
	serial_rows = [r for r in (rows or []) if r.serial_no]
	if serial_rows:
		return [{"serial_no": r.serial_no, "qty": abs(flt(r.qty)) or 1.0,
			"rate": flt(r.incoming_rate), "cost": flt(r.stock_value_difference)} for r in serial_rows]
	legacy = [s.strip() for s in (sle.serial_no or "").splitlines() if s.strip()]
	if legacy:
		each = flt(sle.stock_value_difference) / len(legacy)
		return [{"serial_no": s, "qty": 1.0, "rate": flt(sle.incoming_rate) or each, "cost": each}
			for s in legacy]
	qty = flt(sle.actual_qty)
	cost = flt(sle.stock_value_difference)
	return [{"serial_no": None, "qty": qty, "rate": flt(sle.incoming_rate) or (cost / qty if qty else 0.0),
		"cost": cost}]


def _fetch_acquisitions(sles, bundle_rows) -> dict[str, list]:
	"""serial → its submitted FFL Acquisitions, oldest first."""
	serials: set[str] = set()
	for s in sles:
		for u in _explode(s, bundle_rows):
			if u["serial_no"]:
				serials.add(u["serial_no"])
	if not serials:
		return {}
	out: dict[str, list] = {}
	for r in frappe.get_all("FFL Acquisition", filters={"docstatus": 1, "serial": ("in", list(serials))},
			fields=["name", "serial", "source_type", "source_name", "acquisition_date"],
			order_by="acquisition_date asc, creation asc"):
		out.setdefault(r.serial, []).append(r)
	return out


def _pick_acquisition(candidates, posting_date):
	"""The bound-book entry this receipt corresponds to: the latest acquisition
	dated on/before the receipt (a re-acquired gun has several), else the
	earliest later one (the two are booked together — a clock skew between them
	is not a missing entry). None only when the serial has no acquisition."""
	if not candidates:
		return None
	pd = getdate(posting_date)
	before = [c for c in candidates if c.acquisition_date and getdate(c.acquisition_date) <= pd]
	return before[-1] if before else candidates[0]


# ------------------------------------------------------------- classification

def _classify(sle, header, custody, acq) -> str:
	vt = sle.voucher_type
	if sle.warehouse in custody:
		return CUSTODY
	if vt == "Purchase Receipt":
		acq_type = header.get("ffl_acquisition_type") or ""
		if is_consignment_acquisition_type(acq_type):
			return CONSIGNMENT
		text = f"{header.get('ffl_source') or ''} {acq_type}".lower()
		if "trade" in text:
			return TRADE_IN
		if "return" in text:
			return RETURN
		return PURCHASE
	if vt == "Stock Entry":
		purpose = header.get("purpose") or header.get("stock_entry_type") or ""
		if purpose in _TRANSFER_PURPOSES:
			return TRANSFER
		if purpose == "Material Receipt":
			return TRADE_IN if (acq and acq.source_type == TRADE_IN_SOURCE) else INTAKE
		return OTHER
	if vt in _RETURN_VOUCHERS:
		return RETURN
	if vt == "Stock Reconciliation":
		return ADJUSTMENT
	if vt == "Purchase Invoice":
		return PURCHASE
	return OTHER


def _source(sle, header, receipt_class, acq, transfer_from=None) -> str:
	vt = sle.voucher_type
	if receipt_class == CUSTODY:
		return header.get("transfer_customer_name") or header.get("ffl_transfer_for_customer") \
			or header.get("seller_name") or header.get("supplier_name") or ""
	if vt in ("Purchase Receipt", "Purchase Invoice"):
		return header.get("seller_name") or header.get("supplier_name") or header.get("supplier") or ""
	if vt == "Stock Entry":
		if receipt_class == TRANSFER:
			return _("From {0}").format(transfer_from or header.get("from_warehouse") or "?")
		if acq:
			name = acq.source_name or ""
			# The Type column already says Trade-in; for every other intake the
			# bound-book source type (Individual / Estate / Pawn …) adds information.
			if receipt_class == TRADE_IN or not (name and acq.source_type):
				return name or acq.source_type or ""
			return f"{name} · {acq.source_type}"
		return header.get("remarks") or _("Manual receipt")
	if vt in _RETURN_VOUCHERS:
		return header.get("customer_name") or header.get("customer") or ""
	if vt == "Stock Reconciliation":
		return _("Stock count / correction")
	return ""


def _flags(unit, category, receipt_class) -> str:
	if receipt_class == REVALUATION:
		return ""  # a cost change on a unit already on the books — nothing to receive
	flags = []
	if flt(unit["cost"]) <= _ZERO_COST_CEILING and receipt_class not in (CONSIGNMENT, CUSTODY):
		flags.append(FLAG_NO_COST)
	if category == FIREARM and unit["serial_no"] and not unit["acquisition"]:
		flags.append(FLAG_NO_AD)
	return "; ".join(flags)


def _passes(unit, filters) -> bool:
	cls = unit["receipt_class"]
	if filters.receipt_class:
		if cls != filters.receipt_class:
			return False
	elif cls in _OPT_IN and not frappe.utils.cint(filters.get(_OPT_IN[cls])):
		return False
	if filters.category and unit["category"] != filters.category:
		return False
	if filters.get("supplier") and unit.get("supplier") != filters.supplier:
		return False
	return True


# -------------------------------------------------------------------- columns

def _detail_columns():
	# Render order is reading order: what / how / from whom / cost / flags fit a
	# laptop without scrolling; Unit Cost follows; the identifiers the
	# CPA rarely needs (Item code, A&D link, Item Group) start hidden — Pick
	# Columns brings them back and the export offers "Include hidden columns".
	# Receipt comes first because the tree toggle renders in the first column:
	# the ▸ then sits beside the document it opens, and Date stays untruncated.
	return [
		{"label": _("Receipt"), "fieldname": "voucher_no", "fieldtype": "Dynamic Link",
			"options": "voucher_type", "width": 200},
		{"label": _("Date"), "fieldname": "posting_date", "fieldtype": "Date", "width": 110},
		{"label": _("Type"), "fieldname": "receipt_class", "fieldtype": "Data", "width": 120},
		{"label": _("Source"), "fieldname": "source", "fieldtype": "Data", "width": 160},
		{"label": _("Category"), "fieldname": "category", "fieldtype": "Data", "width": 95},
		{"label": _("Description"), "fieldname": "item_name", "fieldtype": "Data", "width": 190},
		{"label": _("Serial"), "fieldname": "serial_no", "fieldtype": "Link", "options": "Serial No", "width": 125},
		# Int, not Float: stock UOM here is always Nos, and Float ignores a column
		# precision of 0 on screen (renders 46.000).
		{"label": _("Qty"), "fieldname": "qty", "fieldtype": "Int", "width": 55},
		{"label": _("Cost"), "fieldname": "cost", "fieldtype": "Currency", "width": 110},
		{"label": _("Flags"), "fieldname": "flags", "fieldtype": "Data", "width": 85},
		{"label": _("Unit Cost"), "fieldname": "rate", "fieldtype": "Currency", "width": 100},
		{"label": _("Item"), "fieldname": "item_code", "fieldtype": "Link", "options": "Item", "width": 150, "hidden": 1},
		{"label": _("A&D"), "fieldname": "acquisition", "fieldtype": "Link", "options": "FFL Acquisition",
			"width": 135, "hidden": 1},
		{"label": _("Item Group"), "fieldname": "item_group", "fieldtype": "Link", "options": "Item Group",
			"width": 110, "hidden": 1},
	]


def _summary_columns():
	return [
		{"label": _("Category / Type"), "fieldname": "group", "fieldtype": "Data", "width": 230},
		{"label": _("Receipts"), "fieldname": "receipts", "fieldtype": "Int", "width": 90},
		{"label": _("Units"), "fieldname": "qty", "fieldtype": "Int", "width": 80},
		{"label": _("Cost"), "fieldname": "cost", "fieldtype": "Currency", "width": 130},
		{"label": _("No-cost units"), "fieldname": "no_cost", "fieldtype": "Int", "width": 125},
		{"label": _("Missing A&D"), "fieldname": "no_ad", "fieldtype": "Int", "width": 120},
	]


# ---------------------------------------------------------------------- views

_UNIT_FIELDS = ("posting_date", "voucher_type", "voucher_no", "receipt_class", "source",
	"category", "item_code", "item_name", "item_group", "serial_no", "qty", "rate", "cost",
	"acquisition", "flags")


def _unit_row(u, *, indent=None, parent=None) -> dict:
	row = {k: u.get(k) for k in _UNIT_FIELDS}
	row["row_kind"] = "unit"
	if indent is not None:
		row.update({"indent": indent, "row_key": f"u:{u['sle']}:{u['serial_no']}", "parent_row": parent})
		# A unit under its line repeats nothing but what is per-unit.
		row.update({"posting_date": None, "voucher_no": None, "voucher_type": "", "receipt_class": None,
			"source": None, "category": None, "item_code": None, "item_name": None,
			"item_group": None})
	return row


def _join_flags(units) -> str:
	seen: list[str] = []
	for u in units:
		for f in (u.get("flags") or "").split("; "):
			if f and f not in seen:
				seen.append(f)
	return "; ".join(seen)


def _group_row(units, *, indent, key, parent) -> dict:
	"""A voucher or line row: shared facts of its units, totals of their numbers."""
	first = units[0]
	categories = {u["category"] for u in units}
	single = len(units) == 1
	return {
		"posting_date": first["posting_date"], "voucher_type": first["voucher_type"],
		"voucher_no": first["voucher_no"], "receipt_class": first["receipt_class"],
		"source": first["source"],
		"category": first["category"] if len(categories) == 1 else _("Mixed"),
		"item_code": first["item_code"], "item_name": first["item_name"], "item_group": first["item_group"],
		"serial_no": first["serial_no"] if single else None,
		"qty": sum(flt(u["qty"]) for u in units),
		"rate": first["rate"] if single else None,
		"cost": sum(flt(u["cost"]) for u in units),
		"acquisition": first["acquisition"] if single else None,
		"flags": _join_flags(units),
		"indent": indent, "row_key": key, "parent_row": parent,
	}


def _total_row(units, **extra) -> dict:
	row = {"voucher_no": _("Total"), "voucher_type": "", "qty": sum(flt(u["qty"]) for u in units),
		"cost": sum(flt(u["cost"]) for u in units), "is_total": 1, "row_kind": "total"}
	row.update(extra)
	return row


def _receipts_view(units):
	"""Tree: receipt voucher → item lines → serial units. A single-line voucher
	shows its line inline (one row, not two); a single-unit line shows its serial
	inline. Only a line with several serials grows unit children — and those
	start collapsed (collapse_children → .js), so the default screen is one row
	per receipt line with the serial list one click away."""
	by_voucher: OrderedDict = OrderedDict()
	for u in units:
		by_voucher.setdefault((u["voucher_type"], u["voucher_no"]), []).append(u)
	rows = []
	for (_vt, vno), vunits in by_voucher.items():
		lines: OrderedDict = OrderedDict()
		for u in vunits:
			lines.setdefault(u["sle"], []).append(u)
		vkey = f"v:{vno}"
		vrow = _group_row(vunits, indent=0, key=vkey, parent=None)
		vrow["row_kind"] = "voucher"
		rows.append(vrow)
		if len(lines) == 1:
			lunits = next(iter(lines.values()))
			if len(lunits) > 1:
				vrow["collapse_children"] = 1
				rows.extend(_unit_row(u, indent=1, parent=vkey) for u in lunits)
			continue
		vrow.update({"item_code": None, "item_group": None, "rate": None,
			"item_name": _("{0} items").format(len(lines))})
		for sle_name, lunits in lines.items():
			lkey = f"l:{sle_name}"
			lrow = _group_row(lunits, indent=1, key=lkey, parent=vkey)
			lrow.update({"row_kind": "line", "posting_date": None, "voucher_no": None,
				"voucher_type": "", "receipt_class": None, "source": None})
			rows.append(lrow)
			if len(lunits) > 1:
				lrow["collapse_children"] = 1
				rows.extend(_unit_row(u, indent=2, parent=lkey) for u in lunits)
	if units:
		rows.append(_total_row(units, indent=0, row_key="total", parent_row=None))
	return _detail_columns(), rows


def _units_view(units):
	rows = [_unit_row(u) for u in units]
	if units:
		rows.append(_total_row(units))
	return _detail_columns(), rows


def _summary_view(units):
	"""Category → receipt class roll-up. Receipts = distinct vouchers; the two
	flag columns count what the cards count, per bucket, so a red card can be
	traced to its row."""
	def bucket(us):
		return {
			"receipts": len({u["voucher_no"] for u in us}),
			"qty": sum(flt(u["qty"]) for u in us),
			"cost": sum(flt(u["cost"]) for u in us),
			"no_cost": sum(1 for u in us if FLAG_NO_COST in u["flags"]),
			"no_ad": sum(1 for u in us if FLAG_NO_AD in u["flags"]),
		}
	rows = []
	for cat in CATEGORIES:
		cus = [u for u in units if u["category"] == cat]
		if not cus:
			continue
		rows.append({"group": cat, "indent": 0, "row_key": f"c:{cat}", "parent_row": None,
			"row_kind": "category", **bucket(cus)})
		for cls in CLASSES:
			kus = [u for u in cus if u["receipt_class"] == cls]
			if kus:
				rows.append({"group": cls, "receipt_class": cls, "indent": 1, "row_key": f"k:{cat}:{cls}",
					"parent_row": f"c:{cat}", "row_kind": "class", **bucket(kus)})
	if units:
		rows.append({"group": _("Total"), "indent": 0, "row_key": "total", "parent_row": None,
			"is_total": 1, "row_kind": "total", **bucket(units)})
	return _summary_columns(), rows


# ------------------------------------------------------------- cards / message

def _cards(units, unsettled=()):
	def cost_of(cat):
		return sum(flt(u["cost"]) for u in units if u["category"] == cat)
	firearms = sum(flt(u["qty"]) for u in units if u["category"] == FIREARM)
	no_cost = sum(1 for u in units if FLAG_NO_COST in u["flags"])
	no_ad = sum(1 for u in units if FLAG_NO_AD in u["flags"])
	return [
		report_cards.card(sum(flt(u["cost"]) for u in units), _("Cost Received"), "Currency", "Blue"),
		report_cards.card(int(firearms), _("Firearms (units)"), None, "Grey"),
		report_cards.card(cost_of(AMMUNITION), _("Ammunition"), "Currency", "Grey"),
		report_cards.card(cost_of(ACCESSORY), _("Accessories"), "Currency", "Grey"),
		report_cards.flag_card(no_cost, _("No-cost units")),
		report_cards.flag_card(no_ad, _("Missing A&D")),
		report_cards.flag_card(len(unsettled), _("Seller not settled")),
	]


def _message(view, data, filters, unsettled=()) -> str:
	if not data:
		body = _("Nothing entered stock in {0}–{1} for these filters. Widen the dates, clear "
			"Category / Type, or tick Include transfers / custody.").format(
			filters.get("from_date") or "", filters.get("to_date") or "")
	else:
		body = {
			"Summary": _("Cost by category and how it came in. Receipts = distinct documents."),
			"Units": _("One row per unit (per serial for firearms) — the CSV / export grain."),
		}.get(view, _("One row per receipt; ▸ opens its lines, and a line's serials. "
			"Internal transfers and customer-custody guns are hidden unless included."))
	html = f'<span class="text-muted">{body}</span>'
	if unsettled:
		# A private seller's receipt whose seller payment does not match it (cash_drawer.unsettled_seller_receipts):
		# negative = the seller is still owed, positive = paid but the receipt is cancelled.
		html += '<div class="text-danger">{0} {1}</div>'.format(
			_("Seller not settled (receipt: amount):"),
			", ".join(f"{frappe.utils.escape_html(r.name)}{_(' (cancelled)') if r.docstatus == 2 else ''}: {flt(r.left):,.2f}"
				for r in unsettled))
	return html
