"""RSR Ship Restriction — one SKU's slice of RSR's ship-restriction feed.

21,795 rows (one per restricted SKU, its feed lines packed into the `entries`
JSON column), reloaded whenever the file's content hash changes. Written only by
`rsr.loader.load_restrictions_file` (chunked multi-row INSERT) and read only by
`rsr.restrictions`; nothing edits a row by hand, which is why the doctype grants
no create/write permission — a hand edit would be silently overwritten by the
next load, and a UI that offers it is lying.
"""
from __future__ import annotations

import frappe
from frappe.model.document import Document


class RSRShipRestriction(Document):
	pass


def on_doctype_update():
	"""Indexes the read path actually uses.

	Frappe's `search_index` on a single field cannot express the composite, and
	the composite is the whole point: every read is "one SKU, current batch".
	`add_index` is idempotent (`has_index` + `ADD INDEX IF NOT EXISTS`), so this
	is safe on every migrate.

	`rsr_stock_number` therefore carries NO `search_index` of its own in the
	doctype JSON: it is already the leftmost column of the composite below, so a
	second index on it would be maintained on every INSERT and never used. (That
	note used to live in the field's `description`, i.e. in help text shown to
	staff on the form — it belongs here.)

	`load_batch` gets its own index because the batch sweep deletes by it —
	cheap insurance at 22k rows, and what keeps retiring the previous batch from
	scanning the table per chunk.

	The earlier per-line schema declared (rsr_stock_number, state); `state` is
	no longer a doctype field, so that index is dropped here if a site still
	carries it — migrate self-heals dev sites that lived through the reshape.
	Fresh sites never had it and the DROP is skipped.
	"""
	frappe.db.add_index("RSR Ship Restriction", ["rsr_stock_number", "load_batch"])
	frappe.db.add_index("RSR Ship Restriction", ["load_batch"])
	stale = "rsr_stock_number_state_index"
	if frappe.db.sql(
			"""SELECT 1 FROM information_schema.STATISTICS
			WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
			AND INDEX_NAME = %s LIMIT 1""",
			("tabRSR Ship Restriction", stale)):
		frappe.db.sql_ddl(
			f"ALTER TABLE `tabRSR Ship Restriction` DROP INDEX `{stale}`")
