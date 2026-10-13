"""App-layering contract for the distributor layer.

``ffl_integrations`` must stay installable and usable on a site that has **no**
``ffl_woo_sync`` at all — the POS-only shop, which is a real product form (a
store with no webshop). So the dependency graph is strictly one-way:

    ffl_woo_sync  ──(frappe.get_hooks only)──▶  ffl_integrations

``ffl_woo_sync`` never imports ``ffl_integrations`` (a frozen
inversion), and ``ffl_integrations`` never imports ``ffl_woo_sync`` either.
Both directions are pinned here so neither can drift back.

The consequence is that a handful of rules necessarily exist on both sides. The
established precedent applies: duplication is accepted where the import ban forces
it, guarded by an agreement test and reciprocal pointer comments. This file is
that guard.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

import frappe

_APPS = pathlib.Path(__file__).resolve().parents[3]
INTEGRATIONS = _APPS / "ffl_integrations"
WOO_SYNC = _APPS / "ffl_woo_sync"


def _imported_modules(path: pathlib.Path):
	"""Every module named by a real `import` statement, at any nesting depth.

	AST rather than grep: a dotted path inside a STRING (the deliberate late-bind
	this layering forces) is not an import, and a comment mentioning the app is
	not one either. Both would be false positives for a grep, and the second is
	unavoidable — the pointer comments this rule requires name the other app.
	"""
	tree = ast.parse(path.read_text(), filename=str(path))
	found = set()
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			found.update(alias.name for alias in node.names)
		elif isinstance(node, ast.ImportFrom):
			if node.module and not node.level:
				found.add(node.module)
	return found


def _offenders(root: pathlib.Path, forbidden_prefix: str):
	out = []
	for path in sorted(root.rglob("*.py")):
		if "/tests/" in str(path) or path.name.startswith("test_"):
			continue  # a test may import both sides — that is how agreement is checked
		for module in _imported_modules(path):
			if module == forbidden_prefix or module.startswith(forbidden_prefix + "."):
				out.append(f"{path.relative_to(root.parent)} imports {module}")
	return out


class TestAppLayering(unittest.TestCase):

	def test_integrations_never_imports_woo_sync(self):
		"""The direction this PR had to correct.

		A POS-only install has no ffl_woo_sync package on disk, so ANY import of
		it — including one inside a function — is a latent ImportError on a
		supported configuration. The web-fulfilment call sites late-bind by dotted
		path instead (see distributor/tasks.py::woo_attr).
		"""
		offenders = _offenders(INTEGRATIONS, "ffl_woo_sync")
		self.assertEqual(offenders, [], "\n".join(offenders))

	def test_woo_sync_never_imports_integrations(self):
		"""The frozen inversion, pinned in the same place as its mirror."""
		if not WOO_SYNC.exists():  # pragma: no cover — both apps live in this repo
			self.skipTest("ffl_woo_sync not present")
		offenders = _offenders(WOO_SYNC, "ffl_integrations")
		self.assertEqual(offenders, [], "\n".join(offenders))


# The website drop-ship layer (listing distributor products on the website,
# routing web orders to them) is the osa_dropship extension. Core reaches it only
# through the named hooks in distributor/dropship_hooks.py — never by import,
# never by a dotted path or a DocType name — so every core app works without it.
DROPSHIP_MODULES = frozenset({
	"catalog_service", "center", "console", "house_settings", "listing", "listing_price",
	"listing_rules", "listing_sync", "options", "pricing", "rules_engine", "run_record",
	"sourcing", "stock", "web_lanes", "web_orders", "web_router"})
DROPSHIP_DOCTYPE_NAMES = (
	"OSA Dropship", "Distributor Listing", "Distributor Rule Run", "Dropship Pricing Settings",
	"Dropship Pricing Rule", "Dropship Brand MAP Mode")


class TestDropshipLayerBoundary(unittest.TestCase):

	def test_core_never_imports_the_dropship_layer(self):
		"""Anything in the core apps (ffl_core, ffl_integrations, ffl_woo_sync) must
		keep working without the extension installed."""
		offenders = []
		for path in sorted(_APPS.rglob("*.py")):
			rel = str(path.relative_to(_APPS))
			if "/tests/" in rel:
				continue
			for module in sorted(_imported_modules(path)):
				if module == "osa_dropship" or module.startswith("osa_dropship."):
					offenders.append(f"{rel} imports {module}")
		self.assertEqual(offenders, [], "\n".join(offenders))

	def test_core_never_names_the_layer_in_a_string(self):
		"""An enqueue path or a DocType name is a dependency an import scan cannot
		see (a raw `tabDropship Pricing Rule` read broke the hub-without-layer
		case once)."""
		import io
		import tokenize

		offenders = []
		for path in sorted(_APPS.rglob("*.py")):
			rel = str(path.relative_to(_APPS))
			if "/tests/" in rel or "/patches/" in rel:
				continue
			for tok in tokenize.generate_tokens(io.StringIO(path.read_text()).readline):
				if tok.type != tokenize.STRING or tok.string.startswith(('"""', "'''")):
					continue
				if "osa_dropship." in tok.string or any(dt in tok.string for dt in DROPSHIP_DOCTYPE_NAMES):
					offenders.append(f"{rel}:{tok.start[0]} {tok.string[:80]}")
		self.assertEqual(offenders, [], "\n".join(offenders))

	def test_every_distributor_dotted_path_resolves(self):
		"""An enqueue / hook / endpoint path to a function that moved fails only at
		run time (web routing stopped on a stale `router.route_source`)."""
		import re

		pat = re.compile(r"ffl_integrations\.distributor\.[a-z_]+\.[A-Za-z_]+")
		missing = []
		for path in sorted(INTEGRATIONS.rglob("*")):
			rel = str(path.relative_to(_APPS))
			if path.suffix not in (".py", ".js", ".json") or "/tests/" in rel:
				continue
			for dotted in set(pat.findall(path.read_text())):
				try:
					frappe.get_attr(dotted)
				except Exception:  # noqa: BLE001 — any failure to resolve is the finding
					missing.append(f"{rel}: {dotted}")
		self.assertEqual(missing, [], "\n".join(missing))

	def test_the_layer_left_the_core(self):
		"""No drop-ship layer module crept back into the core distributor package."""
		back = [m for m in DROPSHIP_MODULES
			if (INTEGRATIONS / "ffl_integrations" / "distributor" / f"{m}.py").exists()]
		self.assertEqual(back, [])


class TestPaidStatusAgreement(unittest.TestCase):
	"""The paid-status set is one of the rules the import ban duplicates.

	WooCommerce's own `wc_get_is_paid_statuses()` default drives whether we may
	order goods against a customer's money. Getting it wrong in ONE app means
	either ordering against an unpaid order or never routing a paid one, so the
	two copies are checked against each other rather than trusted.
	"""

	def test_the_two_copies_are_identical(self):
		from ffl_integrations.distributor import router
		from ffl_woo_sync.woocommerce import fulfillment

		self.assertEqual(router.PAID_WOO_STATUSES, fulfillment._PAID_STATUSES)

	def test_both_agree_across_the_status_matrix(self):
		from ffl_integrations.distributor import router
		from ffl_woo_sync.woocommerce import fulfillment

		for status in ("processing", "completed", "PROCESSING", " completed ", "awaiting-stock",
				"on-hold", "pending", "failed", "cancelled", "refunded",
				"checkout-draft", "", None):
			self.assertEqual(
				router.is_paid_woo_status(status),
				fulfillment._is_paid_woo_status(status),
				f"disagreement on {status!r}")
		self.assertTrue(router.is_paid_woo_status("awaiting-stock"),
			"the storefront plugin's Awaiting Stock is a paid order waiting for its goods")


class TestDropshipStatusAgreement(unittest.TestCase):
	"""The 'unsettled distributor status' set exists in THREE places and one
	of them (the webhook's auto-clear guard) silently misfires if they drift:

	* ``router.UNSETTLED_DROPSHIP_STATUSES`` — the roll-up authority;
	* ``webhook._UNSETTLED_DROPSHIP_STATUSES`` — the auto-clear guard's copy;
	* the ``dropship_status`` Select options on the Woo Online Order doctype.

	The two sets must be equal, and every value in them must be a real doctype
	option (the doctype also carries Complete + blank, which are settled).
	"""

	def _doctype_options(self):
		import json

		matches = list(WOO_SYNC.rglob("doctype/woo_online_order/woo_online_order.json"))
		self.assertEqual(len(matches), 1, f"doctype JSON not uniquely found: {matches}")
		d = json.loads(matches[0].read_text())
		field = next(f for f in d["fields"] if f["fieldname"] == "dropship_status")
		return {o.strip() for o in field["options"].split("\n") if o.strip()}

	def test_the_two_copies_are_identical(self):
		from ffl_integrations.distributor import router
		from ffl_woo_sync.woocommerce import webhook

		self.assertEqual(
			router.UNSETTLED_DROPSHIP_STATUSES, webhook._UNSETTLED_DROPSHIP_STATUSES)

	def test_every_unsettled_value_is_a_real_doctype_option(self):
		from ffl_integrations.distributor import router

		options = self._doctype_options()
		for value in router.UNSETTLED_DROPSHIP_STATUSES:
			self.assertIn(value, options, f"{value!r} is not a dropship_status option")

	def test_the_settled_values_are_options_too(self):
		"""Complete is a real terminal option; the guard must not accidentally
		include it, but it must exist for the roll-up to reach it."""
		from ffl_integrations.distributor import router

		options = self._doctype_options()
		self.assertIn(router.DS_COMPLETE, options)
		self.assertNotIn(router.DS_COMPLETE, router.UNSETTLED_DROPSHIP_STATUSES)


class TestShortShipDetection(unittest.TestCase):
	"""RSR acking fewer than we ordered (fill-or-kill off) is the common
	case, and v1 must at least ALERT so a human can arrange the partial refund
	(which stays manual — see docs/rsr-fds-after-sale-sop.md). Silent short-ships
	are how a customer pays for goods that never ship."""

	def _line(self, sku, wish, shipped, status="Shipped"):
		# ack_qty deliberately equals wish: under the live contract ack is the
		# placement-time commitment carried forward by every poll, so it can
		# NEVER signal a short ship — invoiced_qty (shipped) is the baseline.
		return {"sku": sku, "wish_qty": wish, "ack_qty": wish,
			"invoiced_qty": shipped, "line_status": status}

	def test_a_settled_line_shipped_below_order_is_short(self):
		from ffl_integrations.distributor import tasks

		short = tasks.short_shipped_lines([self._line("A", 3, 2)])
		self.assertEqual([s["sku"] for s in short], ["A"])
		self.assertEqual(short[0]["short_by"], 1)

	def test_full_ack_is_not_short(self):
		from ffl_integrations.distributor import tasks

		self.assertEqual(tasks.short_shipped_lines([self._line("A", 3, 3)]), [])

	def test_a_still_pending_line_is_not_yet_short(self):
		"""Only a SETTLED line is short — an un-acked line may still be filled."""
		from ffl_integrations.distributor import tasks

		self.assertEqual(
			tasks.short_shipped_lines([self._line("A", 3, 0, status="Pending")]), [])

	def test_a_killed_line_is_short_by_its_whole_quantity(self):
		from ffl_integrations.distributor import tasks

		short = tasks.short_shipped_lines([self._line("A", 2, 0, status="Killed")])
		self.assertEqual(short[0]["short_by"], 2)


class TestUpdateOrderFflAction(unittest.TestCase):
	"""The standard FDS-Hold remedy (attach/replace the ShipFFL) must be
	reachable. The whitelisted action re-validates the new FFL through the SAME
	canonical chain route uses BEFORE calling the provider — you cannot
	release a hold onto an expired licence."""

	def test_a_bad_new_ffl_is_rejected_before_the_provider_is_called(self):
		from unittest.mock import MagicMock, patch

		from ffl_integrations.distributor import router

		provider = MagicMock()
		with patch.object(router, "validate_fds_destination",
				return_value=(None, router.REASON_FFL_EXPIRED)), \
			 patch.object(router, "_do_provider", return_value=provider), \
			 patch("frappe.get_doc"), patch("frappe.only_for"):
			with self.assertRaises(Exception):
				router.update_order_ffl("DO-1", "1-57-BAD")
		provider.update_order_ffl.assert_not_called()

	def test_a_valid_new_ffl_calls_the_provider_and_pins_the_record(self):
		from unittest.mock import MagicMock, patch

		from ffl_integrations.distributor import router

		provider = MagicMock()
		provider.capabilities.return_value = {"update_ffl"}
		provider.update_order_ffl.return_value = {"ok": True}
		do = MagicMock()
		with patch.object(router, "validate_fds_destination",
				return_value=("ATF-9", None)), \
			 patch.object(router, "_do_provider", return_value=provider), \
			 patch("frappe.get_doc", return_value=do), patch("frappe.only_for"):
			res = router.update_order_ffl("DO-1", "1-57-GOOD")
		provider.update_order_ffl.assert_called_once_with("DO-1", "1-57-GOOD")
		self.assertEqual(do.transfer_ffl_record, "ATF-9")
		self.assertEqual(do.transfer_ffl_license, "1-57-GOOD")
		self.assertTrue(do.save.called)
		self.assertTrue(res["result"]["ok"])


class TestFdsDestinationValidation(unittest.TestCase):
	"""B3 — the FDS destination FFL goes through the SAME canonical resolve +
	expiry/address gate the local disposition path uses.

	The failure this closes: a drop-shipped gun ships from RSR straight to a
	license, and there is no cancel API. A self-written 'license non-empty' check
	would ship against an expired or address-incomplete FFL that the local path
	(``dealer_transfer.atf_record_block``) rejects — two paths, one of them unsafe.
	"""

	def _order(self, license="1-99-888-01-2D-09999"):
		import frappe

		return frappe._dict({"name": "WOO-ORD-1", "ffl_license": license})

	def _patch(self, *, resolve, block):
		from unittest.mock import patch

		return (
			patch("ffl_core.compliance.dealer_transfer._resolve_destination_ffl",
				side_effect=resolve),
			patch("ffl_core.compliance.dealer_transfer.atf_record_block",
				side_effect=block))

	def test_valid_ffl_returns_the_resolved_record(self):
		from ffl_integrations.distributor import router

		p1, p2 = self._patch(resolve=lambda lic: "ATF-1", block=lambda rec, **k: None)
		with p1, p2:
			record, reason = router.validate_fds_destination(self._order())
		self.assertEqual(record, "ATF-1")
		self.assertIsNone(reason)

	def test_blank_license_is_ffl_missing(self):
		from ffl_integrations.distributor import router

		record, reason = router.validate_fds_destination(self._order(license=""))
		self.assertIsNone(record)
		self.assertEqual(reason, router.REASON_FFL_MISSING)

	def test_unresolvable_license_parks(self):
		from ffl_core.compliance import dealer_transfer
		from ffl_integrations.distributor import router

		def _boom(lic):
			raise dealer_transfer.frappe.ValidationError("unresolvable")

		p1, p2 = self._patch(resolve=_boom, block=lambda rec, **k: None)
		with p1, p2:
			record, reason = router.validate_fds_destination(self._order())
		self.assertIsNone(record)
		self.assertEqual(reason, router.REASON_FFL_UNRESOLVED)

	def test_expired_ffl_parks_and_never_ships(self):
		from ffl_core.compliance import dealer_transfer
		from ffl_integrations.distributor import router

		p1, p2 = self._patch(
			resolve=lambda lic: "ATF-1",
			block=lambda rec, **k: (dealer_transfer.FFL_BLOCK_EXPIRED, "expired"))
		with p1, p2:
			record, reason = router.validate_fds_destination(self._order())
		self.assertIsNone(record)
		self.assertEqual(reason, router.REASON_FFL_EXPIRED)

	def test_incomplete_address_parks(self):
		from ffl_core.compliance import dealer_transfer
		from ffl_integrations.distributor import router

		p1, p2 = self._patch(
			resolve=lambda lic: "ATF-1",
			block=lambda rec, **k: (dealer_transfer.FFL_BLOCK_INCOMPLETE_ADDRESS, "addr"))
		with p1, p2:
			record, reason = router.validate_fds_destination(self._order())
		self.assertIsNone(record)
		self.assertEqual(reason, router.REASON_FFL_INCOMPLETE)

	def test_every_ffl_reason_has_operator_text(self):
		"""route_queue shows the reason; a code with no text is a blank the operator
		cannot act on."""
		from ffl_integrations.distributor import router

		for reason in (router.REASON_FFL_MISSING, router.REASON_FFL_UNRESOLVED,
				router.REASON_FFL_EXPIRED, router.REASON_FFL_INCOMPLETE):
			self.assertIn(reason, router.REASON_TEXT)
			self.assertTrue(router.REASON_TEXT[reason].strip())


if __name__ == "__main__":
	unittest.main()
