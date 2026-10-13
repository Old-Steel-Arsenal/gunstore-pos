"""Runtime predicates for customer custody — "is this gun ours to sell?"

The field definitions live in ``ffl_core.setup.custody``; the warehouse
resolution in ``ffl_core.warehouses``. This module is the thin, importable
answer every other app asks, so the rule is written once (ffl_woo_sync's four
listing touchpoints and the POS pickers all consult it).

TWO independent keys, and either alone is disqualifying:

  * ``Serial No.is_transfer_gun`` — the flag stamped at receive;
  * the gun's physical location in a warehouse flagged
    ``is_custody_warehouse``.

Checking both is the same defence the consignment code takes (see
``firearm._pos_picker_excluded_serials`` rule 3): a flag and a location can
drift apart — a hand-run stock entry moves a gun without touching flags, a
backfill sets flags without moving stock — and the failure this run exists to
prevent is asymmetric. Wrongly hiding one of the store's own guns costs a
listing until someone notices; wrongly exposing a customer's gun sells property
the store does not own. So the answer is the OR, deliberately.
"""
from __future__ import annotations

import frappe

from ffl_core.firearm import TRANSFER_ACQUISITION_TYPES
from ffl_core.warehouses import custody_warehouse_names


def is_custody_serial(serial_no: str) -> bool:
	"""True when this gun is a customer's — by flag or by location.

	One query per call plus the cached warehouse set; for a LOOP over many
	serials of one item take ``custody_serials`` instead."""
	if not serial_no:
		return False
	row = frappe.db.get_value(
		"Serial No", serial_no, ["is_transfer_gun", "warehouse"], as_dict=True)
	if not row:
		return False
	if row.get("is_transfer_gun"):
		return True
	return bool(row.get("warehouse")) and row["warehouse"] in custody_warehouse_names()


def custody_serials(item_codes) -> set[str]:
	"""Every serial of `item_codes` sitting in a customer-custody warehouse, in
	ONE query — the set form of is_custody_serial's LOCATION half, for callers
	that already hold a serial list and only need the location check added
	(the shared-SKU stock count asks about every serial of a SKU at once).

	Returns an empty set — without querying — on a site that has no custody
	warehouse, which is every site until the first transfer is received."""
	whs = custody_warehouse_names()
	if not whs:
		return set()
	codes = [c for c in dict.fromkeys(
		[item_codes] if isinstance(item_codes, str) else (item_codes or [])) if c]
	if not codes:
		return set()
	# Raw SQL, same rationale as its two siblings above: these three
	# custody lookups are reached from code paths that existing suites cover with
	# a wholesale mock of Frappe's query layer, and they sit on hot paths where
	# get_all's permission/meta overhead buys nothing.
	rows = frappe.db.sql(
		"""SELECT name FROM `tabSerial No`
		   WHERE item_code IN %s AND warehouse IN %s""",
		(tuple(codes), tuple(whs)))
	return {r[0] for r in rows}


def audit_legacy_custody_candidates() -> dict:
	"""READ-ONLY. Find in-stock guns that look like customer transfers received
	before custody existed — i.e. still sitting in sellable stock, unflagged.

	Deliberately reports instead of repairing. Moving a gun into
	custody is a stock movement, and zeroing a cost that already posted to the GL
	is an accounting correction —
	neither is a decision this code may take on its own.
	The owner's expectation is that this returns nothing; the point of running it
	is to find out, not to assume.

	Run it (any site, safe — it writes nothing):

	    bench --site <site> execute ffl_core.custody.audit_legacy_custody_candidates

	Two independent signals, reported separately rather than merged, because
	they mean different things and want different remedies:

	  * ``by_acquisition`` — the strong one. An FFL Acquisition that recorded a
	    transfer (a named pickup customer, or a transaction_type in the transfer
	    vocabulary — note prod spells it **"FFL Transfer"**, which is why this
	    shares ``firearm.TRANSFER_ACQUISITION_TYPES`` rather than hard-coding a
	    literal) whose gun is still Active and NOT in custody. Unambiguous.
	    The pickup-customer key is the more reliable of the two: a production audit
	    found an acquisition carrying NEITHER marker — it was
	    never declared a transfer at all, which is precisely how it was received
	    as ordinary stock, and precisely what the new checkbox now prevents.
	  * ``zero_cost`` — the weak one, a net to catch transfers that were never
	    recorded as such. An Active firearm with no purchase_rate is USUALLY
	    innocent (consignment guns are 0 by design, so they are excluded here,
	    as are guns whose $0 a receiver explicitly CONFIRMED — see
	    ffl_core.setup.zero_cost), but a $0 gun the store cannot account for is
	    worth a human look. Expect false positives; this list is a prompt, not a
	    verdict — though a shorter one since 2026-08-08: every gun still on it
	    is one nobody has ever accounted for, rather than one nobody has
	    accounted for IN THIS FIELD.
	"""
	by_acquisition = frappe.db.sql(
		"""
		SELECT sn.name AS serial, sn.item_code, sn.warehouse,
		       sn.purchase_rate, acq.name AS acquisition,
		       acq.transfer_for_customer AS customer, acq.transaction_type
		FROM `tabFFL Acquisition` acq
		INNER JOIN `tabSerial No` sn ON sn.name = acq.serial
		WHERE acq.docstatus = 1
		  AND sn.status = 'Active'
		  AND COALESCE(sn.is_transfer_gun, 0) = 0
		  AND (
		        (acq.transfer_for_customer IS NOT NULL AND acq.transfer_for_customer != '')
		     OR LOWER(TRIM(COALESCE(acq.transaction_type, ''))) IN %(types)s
		  )
		ORDER BY sn.name
		""",
		{"types": tuple(TRANSFER_ACQUISITION_TYPES)},
		as_dict=True,
	)
	zero_cost = frappe.db.sql(
		"""
		SELECT sn.name AS serial, sn.item_code, sn.warehouse, sn.purchase_rate
		FROM `tabSerial No` sn
		INNER JOIN `tabItem` i ON i.name = sn.item_code AND i.is_firearm = 1
		WHERE sn.status = 'Active'
		  AND COALESCE(sn.is_transfer_gun, 0) = 0
		  AND COALESCE(sn.is_consignment, 0) = 0
		  AND COALESCE(sn.zero_cost_confirmed, 0) = 0
		  AND COALESCE(sn.purchase_rate, 0) <= 0
		ORDER BY sn.name
		""",
		as_dict=True,
	)
	custody_whs = custody_warehouse_names()
	by_acquisition = [r for r in by_acquisition if r.get("warehouse") not in custody_whs]
	zero_cost = [r for r in zero_cost if r.get("warehouse") not in custody_whs]
	return {
		"by_acquisition": by_acquisition,
		"zero_cost": zero_cost,
		"clean": not by_acquisition,
	}


def custody_warehouse_qty(item_code: str) -> int:
	"""How much of `item_code` is sitting in customer-custody warehouses.

	For the NON-serialised half of the shared-SKU stock count: that number is a
	plain Bin sum across every warehouse, so whatever is in custody has to come
	back off it. Returns 0 — without querying — when no custody warehouse
	exists."""
	# Cheapest condition first: no item, no work — and, more importantly, no
	# query. The warehouse lookup used to run unconditionally, which put a
	# Warehouse read on the non-firearm stock path of every Woo push.
	if not item_code:
		return 0
	whs = custody_warehouse_names()
	if not whs:
		return 0
	# Raw SQL for the same two reasons as custody_warehouse_names:
	# this runs on every Woo stock push, and frappe.get_all's meta lookup is
	# answered by frappe.db.get_value — which several suites mock wholesale,
	# corrupting Frappe's Meta object on a cold cache. A summed indexed read
	# needs none of that machinery.
	rows = frappe.db.sql(
		"""SELECT COALESCE(SUM(actual_qty), 0) FROM `tabBin`
		   WHERE item_code = %s AND warehouse IN %s""",
		(item_code, tuple(whs)))
	return int(frappe.utils.flt(rows[0][0]) if rows else 0)
