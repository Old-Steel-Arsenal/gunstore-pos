"""The counter may not sell a rifle a paid web order is already owed.

Since a uniform-price web order stopped guessing which rifle it gets, it holds an
unassigned CLAIM: it owes one unit of the Item and reserves no `Serial No`. Every
counter and consignment gate tests `Serial No.woo_reserved`, which a claim never
sets — so during the window between a paid order landing and staff typing its
serial, those gates saw nothing at all.

The failure that motivates it: two free Mosins, two paid web orders, one claim
each. `stock_for_item` correctly reports 0 and the storefront shows out of stock.
A staff member then sells BOTH rifles at the counter — `woo_reserved` is 0, no
counter hold exists, nothing objects — and two paid web orders become
unfulfillable, with a `frappe.logger` warning the only trace.

The rule has to be UNIT-level, because the claim is. `free <= claims` means every
free rifle is already spoken for, so selling one more leaves `free - 1 < claims`.

Also covers the split this rests on: `counter_holds` (ungated, for decisions) vs
`reserved_counter_serials` (role-gated, for display). The gated one answers `{}`
to a caller without the shipping role, and `{}` reads as "nothing is held" — fine
for a badge, wrong for a gate.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

RES = "ffl_core.reservations"


class TestWebClaimBlockReason(FrappeTestCase):
	"""The web-only wording of the rule. The rule itself — every channel's owed
	units against the shared pool — is covered in `test_shared_pool`; what is
	pinned here is that the ORIGINAL name still means what it did."""

	def _reason(self, claims, free):
		from ffl_core.reservations import web_claim_block_reason

		def _web(_item_code):
			return claims

		_web.channel = "woo"
		with patch(f"{RES}.is_uniform_firearm_item", return_value=True), \
				patch(f"{RES}.uniform_free_serials",
					return_value=[f"S{i}" for i in range(free)]), \
				patch("frappe.get_hooks", side_effect=lambda hook=None, *a, **kw: (
					["x.web"] if hook == "channel_committed_units" else []
				)), \
				patch("frappe.get_attr", return_value=_web):
			return web_claim_block_reason("Mosin 91/30")

	def test_every_free_rifle_already_owed_blocks_the_sale(self):
		"""Two free, two claims: selling one leaves a paid order unfillable."""
		reason = self._reason(2, 2)
		self.assertIsNotNone(reason)
		self.assertIn("Mosin 91/30", reason)

	def test_an_oversold_shelf_blocks(self):
		self.assertIsNotNone(self._reason(3, 2))

	def test_spare_stock_does_not_block(self):
		"""Three free, two claims — one rifle is genuinely the store's to sell."""
		self.assertIsNone(self._reason(2, 3))

	def test_no_claims_never_blocks(self):
		self.assertIsNone(self._reason(0, 0))
		self.assertIsNone(self._reason(0, 5))

	def test_an_item_that_cannot_carry_claims_never_blocks(self):
		"""A non-firearm, a per-serial firearm or a missing Item, so a gate can run
		this over any serial without first asking what kind of Item it is."""
		from ffl_core.reservations import web_claim_block_reason
		with patch(f"{RES}.is_uniform_firearm_item", return_value=False):
			self.assertIsNone(web_claim_block_reason("Mosin 91/30"))

	def test_a_missing_woo_sync_never_blocks(self):
		"""ffl_core must degrade, not fail, when no channel app is installed — a
		counter sale cannot depend on the web app being there."""
		from ffl_core.reservations import web_claim_block_reason
		with patch(f"{RES}.is_uniform_firearm_item", return_value=True), \
				patch("frappe.get_hooks", side_effect=lambda hook=None, *a, **kw: []):
			self.assertIsNone(web_claim_block_reason("Mosin 91/30"))

	def test_a_throwing_channel_never_blocks(self):
		from ffl_core.reservations import web_claim_block_reason
		with patch(f"{RES}.is_uniform_firearm_item", return_value=True), \
				patch("frappe.get_hooks", side_effect=lambda hook=None, *a, **kw: (
					["x.web"] if hook == "channel_committed_units" else [])), \
				patch("frappe.get_attr", return_value=lambda code: 1 / 0), \
				patch("frappe.log_error"):
			self.assertIsNone(web_claim_block_reason("Mosin 91/30"))


class TestCounterHoldsSplit(FrappeTestCase):
	def test_the_gated_wrapper_returns_nothing_without_the_role(self):
		from ffl_core.reservations import reserved_counter_serials
		with patch("ffl_core.api.manual_order._has_ship_role", return_value=False), \
			 patch(f"{RES}.counter_holds") as holds:
			self.assertEqual(reserved_counter_serials("ITEM-1"), {})
		holds.assert_not_called()

	def test_the_gated_wrapper_delegates_when_the_role_is_present(self):
		from ffl_core.reservations import reserved_counter_serials
		with patch("ffl_core.api.manual_order._has_ship_role", return_value=True), \
			 patch(f"{RES}.counter_holds", return_value={"A": {"kind": "pos"}}):
			self.assertEqual(reserved_counter_serials("ITEM-1"), {"A": {"kind": "pos"}})

	def test_the_ungated_helper_is_not_whitelisted(self):
		"""It answers truthfully regardless of role, which is exactly why it must
		not be reachable over HTTP."""
		from ffl_core import reservations
		self.assertNotIn(reservations.counter_holds, frappe.whitelisted)
		self.assertIn(reservations.reserved_counter_serials, frappe.whitelisted)

	def test_no_enforcing_caller_uses_the_gated_wrapper(self):
		"""A gate handed {} concludes 'nothing is held' and lets the gun through;
		a stock count handed {} over-reports the shelf. Only the Item-form badge
		(via xcall) may take the gated one."""
		import pathlib
		import re

		root = pathlib.Path(reservations_root())
		offenders = []
		for path in root.rglob("*.py"):
			if "/tests/" in str(path) or path.name == "reservations.py":
				continue
			for n, line in enumerate(path.read_text().splitlines(), 1):
				if re.search(r"\breserved_counter_serials\s*\(", line):
					offenders.append(f"{path.name}:{n}")
		self.assertEqual(offenders, [], f"enforcing callers on the gated wrapper: {offenders}")


def reservations_root() -> str:
	"""Both app roots — the callers that matter live in ffl_core AND ffl_woo_sync."""
	import ffl_core
	import pathlib
	return str(pathlib.Path(ffl_core.__file__).parent.parent.parent)
