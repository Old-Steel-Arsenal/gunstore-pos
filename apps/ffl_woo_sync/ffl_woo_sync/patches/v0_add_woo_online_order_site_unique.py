"""Add composite UNIQUE index (woo_order_id, site) to Woo Online Order.

FT2 dropped field-level unique:1 on woo_order_id because two WooCommerce stores
can reuse the same order number; uniqueness is now enforced per (woo_order_id, site).
validate() catches duplicates at the app layer, but concurrent webhook deliveries can
race past it. This index gives a DB-level backstop so the second insert is rejected.

Safety guards:
- If the table does not exist yet (fresh install before first migrate), returns early.
- If any existing duplicate (woo_order_id, site) groups are found, logs them and
  skips index creation rather than crashing the migration.
"""
from __future__ import annotations

import frappe

_CONSTRAINT = "unique_woo_order_site"
_DOCTYPE = "Woo Online Order"
_TABLE = f"tab{_DOCTYPE}"


def execute():
	# Guard 1: table must already exist (safe for fresh installs).
	if not frappe.db.table_exists(_DOCTYPE):
		return

	# Guard 2: detect any pre-existing duplicate (woo_order_id, site) pairs.
	duplicates = frappe.db.sql(
		f"""
		SELECT woo_order_id, site, COUNT(*) AS cnt
		FROM `{_TABLE}`
		GROUP BY woo_order_id, site
		HAVING cnt > 1
		""",
		as_dict=True,
	)
	if duplicates:
		frappe.log_error(
			title=f"[{_DOCTYPE}] Skipping unique index — duplicates found",
			message=(
				"Cannot add composite UNIQUE index (woo_order_id, site): "
				"duplicate rows exist. Resolve them manually, then re-run migrate.\n"
				+ "\n".join(
					f"  woo_order_id={d.woo_order_id!r}, site={d.site!r}, count={d.cnt}"
					for d in duplicates
				)
			),
		)
		return

	# frappe.db.add_unique checks information_schema first → idempotent.
	frappe.db.add_unique(_DOCTYPE, ["woo_order_id", "site"], constraint_name=_CONSTRAINT)
