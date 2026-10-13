"""Backfill Serial No.purchase_rate from the source Purchase Receipt's rate.

ERPNext does not reliably auto-stamp Serial No.purchase_rate when serials
arrive via the legacy newline-separated `serial_no` string on a Purchase
Receipt Item (vs Serial-and-Batch Bundle). Existing receives have been
observed with Serial No.purchase_rate=0 + purchase_document_no=NULL even
though the PR Item carries a valid rate.

Walk every submitted PR Item, parse its serial_no string, and stamp each
matching Serial No's purchase_rate (and the PR traceability link) when
purchase_rate is currently 0/empty. Idempotent — re-runs skip already-set
records.
"""
from __future__ import annotations

import frappe


def execute():
	# purchase_document_no exists on some ERPNext v16 builds but not others
	# (present on the dev site this code was written on; absent on the CI
	# base image of the same version). Probe once and write only the columns
	# the schema actually has.
	sn_cols = set(frappe.db.get_table_columns("Serial No"))
	can_stamp_doc_no = "purchase_document_no" in sn_cols
	rows = frappe.db.sql(
		"""
		SELECT pri.serial_no, pri.rate, pri.parent
		FROM `tabPurchase Receipt Item` pri
		WHERE pri.docstatus = 1
		  AND pri.serial_no IS NOT NULL AND pri.serial_no != ''
		  AND pri.rate > 0
		""",
		as_dict=True,
	)
	stamped = 0
	for row in rows:
		serials = [s.strip() for s in (row["serial_no"] or "").splitlines() if s.strip()]
		for serial in serials:
			if not frappe.db.exists("Serial No", serial):
				continue
			cur = frappe.db.get_value("Serial No", serial, "purchase_rate")
			if cur and float(cur) > 0:
				continue  # already set; don't clobber a manually-corrected value
			updates = {"purchase_rate": float(row["rate"])}
			if can_stamp_doc_no:
				updates["purchase_document_no"] = row["parent"]
			frappe.db.set_value(
				"Serial No", serial, updates, update_modified=False,
			)
			stamped += 1
	if stamped:
		print(f"backfill_serial_purchase_rate: stamped {stamped} Serial No record(s)")
