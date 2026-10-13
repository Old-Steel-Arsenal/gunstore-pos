"""Backfill consignment flags on Serial No from FFL Acquisition.

NOT run automatically — deliberately NOT registered in patches.txt (see the
commented-out entry there), so this does not fire on `bench migrate`.

It used to run automatically, stamping every unflagged in-stock consignment
Serial No with the flat store-default commission % (DEFAULT_COMMISSION_PCT).
The owner confirmed that premise is wrong for the real, pre-existing
consignment guns: each was actually consigned at its OWN agreed commission,
not a flat default. Auto-stamping the default on every migrate would (a)
apply the wrong commission going forward, and (b) for guns that had ALREADY
SOLD, silently retro-change what the Sales Report reports as profit for that
sale with no corresponding GL entry — the consignor-payable JE (if any) was
posted at sale time against whatever the Serial No held THEN, so rewriting
the Serial No afterward desyncs report vs. GL rather than fixing anything.

Correcting the existing guns is instead a deliberate, one-time, MANUAL data
operation, done with the owner's real per-gun commissions: for
each gun, stamp Serial No.consignment_commission_pct with its actual agreed
%, not DEFAULT_COMMISSION_PCT — and for any that already sold before the
correction, separately book a retroactive GL adjustment so the payable/COGS
matches the real commission. That is inherently owner-driven and per-gun; it
cannot be safely automated as a blanket migrate step.

The module is kept (not deleted): the flagging logic itself — join FFL
Acquisition by transaction_type="Consignment", find-or-create the consignor
Supplier, stamp is_consignment + consignor onto the Serial No — is still
correct and reusable as the starting point for that manual operation. Only
"default the commission % from the flat store setting" must not run
unattended; a manually-triggered pass (e.g. `bench execute` with
owner-approved per-serial commissions) should override
consignment_commission_pct per gun instead of trusting this default. Idempotent
— never clobbers an already-flagged Serial No.

Fixture-ordering caveat: this is written as a post_model_sync patch, which
can run before the consignment custom fields (created via
fixtures/after_migrate) are guaranteed to exist on a fresh deploy — so
execute() calls setup.consignment.ensure() itself before reading/writing
those columns. Moot for an ordinary `bench migrate` now that the patch isn't
registered, but still matters if this is ever invoked manually before
after_migrate has run.
"""
from __future__ import annotations

import frappe


def execute():
	from ffl_core.setup.consignment import DEFAULT_COMMISSION_PCT, ensure

	ensure()
	default_pct = (
		frappe.db.get_single_value("FFL Settings", "consignment_default_commission_pct")
		or DEFAULT_COMMISSION_PCT
	)
	rows = frappe.db.sql(
		"""
		SELECT acq.serial, acq.source_name, acq.source_customer
		FROM `tabFFL Acquisition` acq
		WHERE acq.docstatus = 1 AND acq.transaction_type = 'Consignment'
		  AND acq.serial IS NOT NULL AND acq.serial != ''
		ORDER BY acq.acquisition_date DESC, acq.creation DESC
		""",
		as_dict=True,
	)
	from ffl_core.consignment import ensure_consignor_supplier

	stamped = 0
	for r in rows:
		serial = (r["serial"] or "").strip()
		if not serial or not frappe.db.exists("Serial No", serial):
			continue
		if frappe.db.get_value("Serial No", serial, "is_consignment"):
			continue  # already flagged
		try:
			consignor = ensure_consignor_supplier(r.get("source_name"), r.get("source_customer"))
		except Exception:
			continue  # no usable consignor name -> skip, leave unflagged
		frappe.db.set_value(
			"Serial No", serial,
			{"is_consignment": 1, "consignment_commission_pct": float(default_pct), "consignor": consignor},
			update_modified=False,
		)
		stamped += 1
	if stamped:
		print(f"backfill_consignment_serials: stamped {stamped} Serial No record(s)")
