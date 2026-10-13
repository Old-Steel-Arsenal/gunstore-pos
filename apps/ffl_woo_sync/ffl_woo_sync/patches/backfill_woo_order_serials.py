"""Seed Woo Online Order Serial rows from the legacy single ``line.serial``.

Each existing firearm line yields one serial row per ordered unit: unit 0 carries
the old ``line.serial`` as ``confirmed_serial`` (best-effort — it is what the order
currently points at), and any additional units are blank, surfacing previously
hidden missing serials. The patch never changes ``Serial No.woo_reserved`` flags,
so WooCommerce stock is unchanged by the migration; staff reconcile visible gaps.
"""
from __future__ import annotations

import frappe

from ffl_core.firearm import is_regulated_firearm_item


def execute():
	for name in frappe.get_all("Woo Online Order", pluck="name"):
		doc = frappe.get_doc("Woo Online Order", name)
		if doc.get("serials"):
			continue  # already migrated
		changed = False
		for line in doc.lines:
			if not is_regulated_firearm_item(line.item_code):
				continue
			qty = max(1, int(line.qty or 1))
			existing_serial = (line.serial or "").strip()
			for unit_index in range(qty):
				doc.append("serials", {
					"item_code": line.item_code,
					"woo_line_id": line.woo_line_id,
					"unit_index": unit_index,
					"confirmed_serial": existing_serial if unit_index == 0 else "",
					"stock_hold_serial": "",
					"reservation_error": "",
				})
				changed = True
		if changed:
			doc.flags.ignore_permissions = True
			doc.save()
