"""Assign explicit Distributor.priority to pre-existing rows.

Before the hub page, planner column order and the default-house pick sorted
"modified asc" — an IMPLICIT order any save would shuffle. This stamps that
same order (oldest first) as explicit 1..N priorities so behaviour is
byte-identical across the cutover. Idempotent: rows that already carry a
non-zero priority are left alone; unset rows continue numbering after the
current max.
"""

import frappe


def execute():
	rows = frappe.get_all(
		"Distributor", fields=["name", "priority"], order_by="modified asc")
	nxt = max((r.priority or 0 for r in rows), default=0) + 1
	for r in rows:
		if r.priority:
			continue
		frappe.db.set_value(
			"Distributor", r.name, "priority", nxt, update_modified=False)
		nxt += 1
