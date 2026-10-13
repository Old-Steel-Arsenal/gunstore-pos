"""Every gb_* column the GunBroker code touches must actually exist.

A stubbed database never validates a column name, so a typo'd or forgotten
field sails through unit tests and fails on the bench with a bare
`OperationalError 1054: Unknown column`. That has cost this repo a red gate
before, which is why `docs/testing.md` asks for a doctype column contract
wherever code reads or writes a field list.

This is that contract for the GunBroker channel, checked statically: the field
names are read out of the source with the AST, and the fixture JSON is read off
disk. Nothing here needs a site, so it fails on a laptop rather than in CI.

Deliberately a whitelist-free scan. Listing the fields by hand in a test is the
same information written twice, and the copy that rots is always the one nobody
looks at.
"""
from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

import ffl_integrations

_INT_ROOT = Path(ffl_integrations.__file__).resolve().parent
_GUNBROKER = _INT_ROOT / "gunbroker"
_FIXTURES = _INT_ROOT / "fixtures" / "custom_field.json"
_SETTINGS_JSON = (_INT_ROOT / "ffl_integrations" / "doctype"
	/ "gunbroker_settings" / "gunbroker_settings.json")

# gb_* names that are not database columns anywhere, so looking for one would
# be looking for something that was never meant to exist. Each is a key in a
# dict the code builds, a dedup key, or a prefix.
#
# GunBroker Order's own columns are NOT listed here: they are read out of its
# DocType JSON below, so that doctype gets the same contract the custom fields
# get rather than an allowlist that stops checking anything.
NOT_SERIAL_FIELDS = frozenset({
	# Alert dedup keys and log prefixes.
	"gb_dup_listing_", "gb_scan_unusable_", "gb_end_", "gb_delete_unauthorized",
	"gb_sold_unmatched_", "gb_conflict_", "gb_listing_sweep",
	"gb_ended_externally_", "gb_sweep_circuit_break",
	"gb_audit_", "gb_payment_diff_", "gb_ack_unauthorized", "gb_layaway_",
	"gb_order_stuck_", "gb_order_sync", "gb_refund_", "gb_unmatched_",
	"gb_invoice_draft_", "gb_overpaid_", "gb_ship_writeback_",
	"gb_gateway_payment_unexpected", "gb_end_pending_dependency_missing",
	"gb_orders_backfill", "gb_receipt_overwritten_", "gb_cancel_",
	"gb_accrual_unreversed_", "gb_rebook_failed_", "gb_antique_not_issued_",
	"gb_oversold_", "gb_unit_dropped_",
	# A model listing ended because its Item stopped being a uniform-price gun.
	"gb_model_ended_",
	# Savepoint name for the amendment re-booking.
	"gb_rebook_amendment",
	# Savepoint name prefix for the per-order poll.
	"gb_ord_",
	# The de-duplicated background jobs the three polls enqueue.
	"gb-order-sync", "gb-tracking-writeback", "gb-end-pending",
	# The midnight price sweep's job name (also its de-duplication id).
	"gb_model_price_sweep",
	# The `kind` a hold registry row reports to the Item form's badge — a
	# display label, not a column (the column it describes is gb_reserved).
	"gb_sold",
	# Keys in the answer a whitelisted endpoint returns to the browser.
	"gb_url",
	# `frappe.flags` key — this channel's own echo guard, deliberately not
	# shared with the WooCommerce one (see gunbroker/doc_events._skip).
	"gb_sync_inbound",
	# The `kind` on a hold registry row, beside "woo_assigned" / "pos" — it
	# names what the Item form's Pending badge should say, not a column.
	"gb_sold",
})


def _serial_fixture_fields() -> set[str]:
	rows = json.loads(_FIXTURES.read_text(encoding="utf-8"))
	return {r["fieldname"] for r in rows if r.get("dt") == "Serial No"}


def _item_fixture_fields() -> set[str]:
	rows = json.loads(_FIXTURES.read_text(encoding="utf-8"))
	return {r["fieldname"] for r in rows if r.get("dt") == "Item"}


def _customer_fixture_fields() -> set[str]:
	rows = json.loads(_FIXTURES.read_text(encoding="utf-8"))
	return {r["fieldname"] for r in rows if r.get("dt") == "Customer"}


def _order_doctype_fields() -> set[str]:
	"""Fields on the GunBroker Order three-file doctype and its child tables.

	Read from the JSON for the same reason the custom fields are: the order
	pipeline writes to these by name, and a typo is a bare "Unknown column" on
	the bench that no stubbed test can see.
	"""
	root = _INT_ROOT / "ffl_integrations" / "doctype"
	names: set[str] = set()
	for slug in ("gunbroker_order", "gunbroker_order_line", "gunbroker_order_serial",
			"gunbroker_settings", "gunbroker_category_map", "gunbroker_fee_tier"):
		path = root / slug / f"{slug}.json"
		if path.is_file():
			schema = json.loads(path.read_text(encoding="utf-8"))
			names |= {f["fieldname"] for f in schema.get("fields", [])}
	return names


def _gb_string_literals() -> dict[str, set[str]]:
	"""Every `gb_…` string literal in the package, by file."""
	out: dict[str, set[str]] = {}
	for path in sorted(_GUNBROKER.rglob("*.py")):
		found = set()
		for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
			if isinstance(node, ast.Constant) and isinstance(node.value, str):
				text = node.value
				if text.startswith("gb_") and text not in NOT_SERIAL_FIELDS:
					found.add(text)
		if found:
			out[path.name] = found
	return out


def _settings_docfields() -> dict[str, dict]:
	schema = json.loads(_SETTINGS_JSON.read_text(encoding="utf-8"))
	return {f["fieldname"]: f for f in schema["fields"]}


class ListingFlagDefaultsMatchTheDocType(unittest.TestCase):
	"""The defaults table must be derived from the DocType, not remembered.

	`single_flags` is handed a `{field: default}` table, and its docstring says
	so plainly: "pass the same defaults the DocType JSON declares, or this
	quietly reintroduces the bug it exists to prevent". That table is hand-copied
	from the JSON, so the copy needs a check against the original — the previous
	version of this compared the implementation's copy with the test fixture's
	copy, which both being wrong satisfies.

	This is drift protection, not a live defect: the twelve entries agree with
	the JSON today. It matters the moment a Check field is ADDED to Settings,
	which PR-2 will do — by then the seeding patch has already run once and will
	not run again, so the read-side default is the only thing left standing
	between an upgraded site and a listing that says no FFL is required.
	"""

	def test_the_settings_json_was_found(self):
		self.assertTrue(_SETTINGS_JSON.is_file(), _SETTINGS_JSON)
		self.assertTrue(_settings_docfields())

	def test_every_default_matches_the_doctype(self):
		from ffl_integrations.gunbroker import mapping

		docfields = _settings_docfields()
		for field, default in mapping.LISTING_FLAG_DEFAULTS.items():
			with self.subTest(field):
				docfield = docfields.get(field)
				self.assertIsNotNone(docfield,
					f"{field} is in LISTING_FLAG_DEFAULTS but not on the DocType")
				self.assertEqual(docfield["fieldtype"], "Check",
					f"{field} is not a Check — only Checks need this treatment, "
					f"because only Checks get cast through cint() on load")
				self.assertEqual(int(docfield.get("default") or 0), int(default),
					f"{field} defaults to {docfield.get('default')!r} on the DocType "
					f"but {default!r} in LISTING_FLAG_DEFAULTS")

	def test_every_flag_the_payload_reads_is_declared(self):
		"""A payload field read out of the flags dict but missing from the table
		is a KeyError at push time — the table IS the contract."""
		from ffl_integrations.gunbroker import mapping

		source = (_GUNBROKER / "mapping.py").read_text(encoding="utf-8")
		used = set()
		for node in ast.walk(ast.parse(source)):
			# flags["is_ffl_required"] and flags.get("card_checkout_enabled")
			if isinstance(node, ast.Subscript) and _is_flags(node.value):
				if isinstance(node.slice, ast.Constant):
					used.add(node.slice.value)
			elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
					and node.func.attr == "get" and _is_flags(node.func.value)
					and node.args and isinstance(node.args[0], ast.Constant)):
				used.add(node.args[0].value)
		self.assertTrue(used, "no flag reads found — has the access pattern changed?")
		missing = sorted(used - set(mapping.LISTING_FLAG_DEFAULTS))
		self.assertEqual(missing, [], f"read from flags but never declared: {missing}")

	def test_every_declared_flag_is_a_check_on_the_form(self):
		"""The reverse direction: a stale entry left behind after a field is
		renamed would silently stop protecting anything."""
		from ffl_integrations.gunbroker import mapping

		docfields = _settings_docfields()
		stale = [f for f in mapping.LISTING_FLAG_DEFAULTS if f not in docfields]
		self.assertEqual(stale, [])


def _is_flags(node) -> bool:
	return isinstance(node, ast.Name) and node.id == "flags"


class GunBrokerFieldContract(unittest.TestCase):
	def test_the_fixture_file_was_found(self):
		self.assertTrue(_FIXTURES.is_file(), _FIXTURES)
		self.assertTrue(_serial_fixture_fields(), "no Serial No custom fields found")

	def test_every_gb_field_the_code_touches_exists_somewhere(self):
		"""Serial No, Item, Customer, or the GunBroker doctypes themselves —
		`gb_category_id` is read off the parent Item row, `gb_user_id` off the
		Customer, the order pipeline's fields off GunBroker Order, and every
		other gb_* name off the serial."""
		declared = (_serial_fixture_fields() | _item_fixture_fields()
			| _customer_fixture_fields() | _order_doctype_fields())
		missing: list[str] = []
		for filename, names in _gb_string_literals().items():
			for name in sorted(names):
				# Skip anything that is plainly a message or a key fragment rather
				# than a column: real fieldnames have no spaces or braces.
				if " " in name or "{" in name:
					continue
				if name not in declared:
					missing.append(f"{filename}: {name}")
		self.assertEqual(missing, [], "\n".join([
			"gb_* names used in code with no matching Custom Field fixture.",
			"On the bench this is `OperationalError 1054: Unknown column`, which",
			"no stubbed test can see. Add the fixture, or add the name to",
			"NOT_SERIAL_FIELDS if it is not a Serial No column.", *missing]))

	def test_the_fields_the_state_reader_selects_all_exist(self):
		"""The SELECT list specifically — a wrong name here throws on every read
		of the channel's state, not only on a write path."""
		from ffl_integrations.gunbroker import listings

		declared = _serial_fixture_fields() | {"item_code", "status"}
		missing = [f for f in listings._SERIAL_STATE_FIELDS if f not in declared]
		self.assertEqual(missing, [], f"serial_state selects unknown columns: {missing}")

	def test_the_item_override_field_exists(self):
		self.assertIn("gb_category_id", _item_fixture_fields())

	def test_every_gunbroker_fixture_is_owned_by_this_app(self):
		"""Fixtures are exported by `module == "FFL Integrations"`. A row with the
		wrong module is simply never exported — it works on the machine that
		created it and exists nowhere else."""
		rows = json.loads(_FIXTURES.read_text(encoding="utf-8"))
		wrong = [r["name"] for r in rows
			if r.get("fieldname", "").startswith(("gb_", "sn_section_gunbroker"))
			and r.get("module") != "FFL Integrations"]
		self.assertEqual(wrong, [])

	def test_the_fixture_file_keeps_the_style_this_repo_already_uses(self):
		"""Pins THIS FILE's existing style, which is not what bench would write.

		The file has always been indent=1 with non-ASCII escaped. Hand-editing it
		with different settings rewrites every line containing an em dash — lines
		nobody touched — and the churn surfaces later as a diff its author cannot
		explain. That already happened once here.

		It is worth being explicit that this is a house style, not the framework's:
		`frappe.utils.fixtures.export_json` writes through `frappe.as_json(...,
		ensure_ascii=False)`, which also sorts keys. So a real `bench
		export-fixtures` WILL reformat this file wholesale and fail this test —
		at which point the reformat is a deliberate decision to review, not a
		regression to fix. Do not "repair" it by loosening the assertion.
		"""
		raw = _FIXTURES.read_text(encoding="utf-8")
		rows = json.loads(raw)
		self.assertEqual(raw, json.dumps(rows, indent=1, ensure_ascii=True) + "\n",
			"custom_field.json has drifted from this repo's existing formatting "
			"(indent=1, ensure_ascii=True, trailing newline). If a real "
			"`bench export-fixtures` produced the change, that is a whole-file "
			"reformat to review deliberately — see this test's docstring.")

	def test_the_state_fields_are_read_only_and_not_copied(self):
		"""These are a record of what GunBroker did. Hand-editing one, or letting
		it ride onto an amended document, would make the local state disagree with
		the marketplace in a way nothing detects."""
		# The gb_* columns a person types into — what the listing should say,
		# decided at Send, as opposed to what the marketplace did.
		inputs = {"gb_ship_cost", "gb_free_shipping", "gb_listing_title", "gb_list_price"}
		rows = json.loads(_FIXTURES.read_text(encoding="utf-8"))
		offenders = []
		for row in rows:
			name = row.get("fieldname", "")
			if row.get("dt") != "Serial No" or not name.startswith("gb_"):
				continue
			if not row.get("read_only") and name not in inputs:
				offenders.append(f"{name} is editable")
			if not row.get("no_copy"):
				offenders.append(f"{name} is copied onto amended documents")
		self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
	unittest.main()


class TheTwoHandledStampsAreUsableByAHuman(unittest.TestCase):
	"""`chargeback_handled_at` and `refund_handled_at` are the only way to stop
	a daily alert, and each one has three separate ways to fail silently.

	Deleted from `fields` — the column goes on the next migrate, the audit's
	query raises "Unknown column", and the per-check `except` swallows it: that
	category simply stops running, on a scheduler, with nobody told.

	Dropped from `field_order` — the column survives, so nothing raises and
	every test still passes, but the field is not on the form. The operator has
	no way to set it and the alert repeats for ever, which is the exact
	condition the stamp was added to end.

	Marked `read_only` — same outcome by a different route, and the more likely
	one: every OTHER field in that section is read-only (they mirror GunBroker),
	so making these match looks like tidying up.
	"""

	def _order_schema(self):
		path = (_INT_ROOT / "ffl_integrations" / "doctype" / "gunbroker_order"
			/ "gunbroker_order.json")
		return json.loads(path.read_text(encoding="utf-8"))

	def test_both_stamps_are_real_fields(self):
		names = {f["fieldname"] for f in self._order_schema()["fields"]}
		self.assertIn("chargeback_handled_at", names)
		self.assertIn("refund_handled_at", names)

	def test_both_stamps_are_laid_out_on_the_form(self):
		order = self._order_schema()["field_order"]
		self.assertIn("chargeback_handled_at", order)
		self.assertIn("refund_handled_at", order)

	def test_neither_stamp_is_read_only(self):
		fields = {f["fieldname"]: f for f in self._order_schema()["fields"]}
		for name in ("chargeback_handled_at", "refund_handled_at"):
			self.assertFalse(fields[name].get("read_only"),
				f"{name} is read-only — nobody can stop its alert")

	def test_the_audit_reads_the_refund_stamp_by_that_exact_name(self):
		# The other half of the pin: a field nothing reads is dead weight, and a
		# read of a name the schema does not have is an Unknown column.
		source = (_GUNBROKER / "audit.py").read_text(encoding="utf-8")
		self.assertIn("refund_handled_at", source)
