"""Endpoint-gate hardening (osa/endpoint-gates adjudication, 2026-08-07).

Every module-level ``@frappe.whitelist`` endpoint in ffl_integrations must open with a
recognized gate as its first executable statement, unless on the reviewed
OPEN_BY_DESIGN allowlist (guest webhooks exempt). Scanner lives in
ffl_core.gate_audit — shared across all three apps' gate tests (reuse
audits flagged the triplicate copy).
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.gate_audit import find_shadowed_endpoints, find_ungated_endpoints

OPEN_BY_DESIGN = {
	# deliberately open catalog reads (counter Create Order panel
	# depends on them; catalog data carries no PII; audits
	# reviewed and upheld).
	"ffl_integrations.distributor.api.search",
	"ffl_integrations.distributor.api.quote",
	"ffl_integrations.distributor.api.check_availability",
	"ffl_integrations.distributor.api.fulfillment_matrix",
	# Catalog row for a scanned UPC (Receive Goods create-from-catalog): the same
	# PII-free catalog data `search` serves, for the same desk users.
	"ffl_integrations.distributor.api.catalog_by_upc",
	# Which features this site has set up (catalog / category tree / national FFL
	# search / update feed / AI): names features, no data — the same dict every
	# desk user already gets in frappe.boot.ffl_capabilities.
	"ffl_integrations.data_service.get_capabilities",
}


class TestGateIsFirstStatement(FrappeTestCase):
	def test_every_whitelist_endpoint_opens_with_a_gate(self):
		offenders = find_ungated_endpoints(
			"ffl_integrations", frappe.get_app_path("ffl_integrations"),
			open_by_design=OPEN_BY_DESIGN)
		self.assertFalse(
			offenders,
			"whitelisted endpoints without a first-statement gate "
			"(add the gate, or add to OPEN_BY_DESIGN with a review note):\n"
			+ "\n".join(offenders))


class TestWrapperBodiesUngated(FrappeTestCase):
	"""The wrapper+_body splits exist so internal callers (provider adapters,
	on_submit hooks) reach the logic without the HTTP wrapper's role gate.
	Guard by AST that the gate-free bodies stay gate-free — a future edit
	that re-inlines only_for into a body fails here."""

	BODIES = {
		"ffl_integrations.rsr.promote": "_promote_to_item",
		"ffl_integrations.atf.ez_check": "_lookup_and_create",
	}

	def test_body_functions_exist_and_have_no_role_gate(self):
		import ast
		import importlib
		from pathlib import Path
		for module, fn in self.BODIES.items():
			mod = importlib.import_module(module)
			self.assertTrue(hasattr(mod, fn), f"{module}.{fn} missing")
			src = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
			node = next(n for n in src.body
				if isinstance(n, ast.FunctionDef) and n.name == fn)
			calls = {ast.unparse(c.func) for c in ast.walk(node)
				if isinstance(c, ast.Call)}
			self.assertNotIn("frappe.only_for", calls,
				f"{module}.{fn} must stay gate-free (internal callers rely on it)")


class TestEndpointsStillResolveToThemselves(FrappeTestCase):
	def test_no_endpoint_is_shadowed_by_a_later_assignment(self):
		"""A module-level assignment to an endpoint's own name replaces it:
		Frappe resolves /api/method/<dotted> by attribute, so the call then
		reaches an un-whitelisted object and 403s while the decorator still
		reads fine in the source. That is how
		ffl_core.api.atf_link.create_party_from_atf was dead for a week."""
		offenders = find_shadowed_endpoints("ffl_integrations", frappe.get_app_path("ffl_integrations"))
		self.assertFalse(
			offenders,
			"whitelisted endpoints shadowed by a later assignment "
			"(rename the alias, not the endpoint):\n" + "\n".join(offenders))
