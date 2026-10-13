"""Endpoint-gate hardening (osa/endpoint-gates adjudication, 2026-08-07).

Every module-level ``@frappe.whitelist`` endpoint in ffl_woo_sync must open with a
recognized gate as its first executable statement, unless on the reviewed
OPEN_BY_DESIGN allowlist (guest webhooks exempt). Scanner lives in
ffl_core.gate_audit — shared across all three apps' gate tests (it used to be
copied into each).
"""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core.gate_audit import find_shadowed_endpoints, find_ungated_endpoints

OPEN_BY_DESIGN = {

}


class TestGateIsFirstStatement(FrappeTestCase):
	def test_every_whitelist_endpoint_opens_with_a_gate(self):
		offenders = find_ungated_endpoints(
			"ffl_woo_sync", frappe.get_app_path("ffl_woo_sync"),
			open_by_design=OPEN_BY_DESIGN)
		self.assertFalse(
			offenders,
			"whitelisted endpoints without a first-statement gate "
			"(add the gate, or add to OPEN_BY_DESIGN with a review note):\n"
			+ "\n".join(offenders))


class TestEndpointsStillResolveToThemselves(FrappeTestCase):
	def test_no_endpoint_is_shadowed_by_a_later_assignment(self):
		"""A module-level assignment to an endpoint's own name replaces it:
		Frappe resolves /api/method/<dotted> by attribute, so the call then
		reaches an un-whitelisted object and 403s while the decorator still
		reads fine in the source. That is how
		ffl_core.api.atf_link.create_party_from_atf was dead for a week."""
		offenders = find_shadowed_endpoints("ffl_woo_sync", frappe.get_app_path("ffl_woo_sync"))
		self.assertFalse(
			offenders,
			"whitelisted endpoints shadowed by a later assignment "
			"(rename the alias, not the endpoint):\n" + "\n".join(offenders))
