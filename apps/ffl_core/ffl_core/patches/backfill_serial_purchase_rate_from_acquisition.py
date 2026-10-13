"""Backfill Serial No.purchase_rate from the firearm's FFL Acquisition cost.

Trade-in and hand-received (individual) intakes go through api/firearm_intake,
which books the gun into stock via a Material Receipt whose basic_rate carries
the acquisition cost — but ERPNext does not flow that onto Serial
No.purchase_rate, and the older backfill_serial_purchase_rate patch only walks
Purchase Receipts. Result: traded-in / hand-received guns sit at
purchase_rate=0 and resell at 0 COGS / 100% margin on the Sales Report even
though their cost is on file. firearm_intake._stamp_serial now stamps it going
forward; this recovers guns taken in before that fix.

The FFL Acquisition is the authoritative per-gun cost for every intake path
(compliance/purchase stamps it from the PR rate, firearm_intake from the entered
acquisition_cost), so stamp purchase_rate from FFL Acquisition.cost wherever a
submitted acquisition has cost > 0 and its Serial No is still 0/empty.

cost > 0 deliberately skips consignment (booked at $0 — unpaid until sold) and
opening-migration guns (no real cost recorded, in prod cost=0 on the
acquisition): those correctly stay flagged as missing on the report rather than
getting a fake near-zero cost. Newest acquisition per serial wins (re-acquired
guns carry the current cost). Idempotent — never clobbers an already-set rate.
"""
from __future__ import annotations

import frappe


def execute():
	rows = frappe.db.sql(
		"""
		SELECT acq.serial, acq.cost
		FROM `tabFFL Acquisition` acq
		WHERE acq.docstatus = 1
		  AND acq.cost > 0
		  AND acq.serial IS NOT NULL AND acq.serial != ''
		ORDER BY acq.acquisition_date DESC, acq.creation DESC
		""",
		as_dict=True,
	)
	stamped = 0
	for row in rows:
		serial = (row["serial"] or "").strip()
		if not serial or not frappe.db.exists("Serial No", serial):
			continue
		cur = frappe.db.get_value("Serial No", serial, "purchase_rate")
		if cur and float(cur) > 0:
			continue  # already set (newest acq already applied, or stamped at intake)
		frappe.db.set_value(
			"Serial No", serial, "purchase_rate", float(row["cost"]),
			update_modified=False,
		)
		stamped += 1
	if stamped:
		print(f"backfill_serial_purchase_rate_from_acquisition: stamped {stamped} Serial No record(s)")
