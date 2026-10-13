"""The Catalog API contract runner against the in-memory reference — the
document (docs/catalog-api/README.md), the reference stub and the runner must
agree, and a service that breaks a rule must be caught."""
from __future__ import annotations

import unittest

from ffl_integrations.catalog_contract import check
from ffl_integrations.catalog_contract.transports import StubTransport
from ffl_integrations.tests import catalog_stub


def _seeded():
	stub = catalog_stub.CatalogStub()
	stub.upsert("rsr", catalog_stub._normalize({"rsr_stock_number": "GLK19", "upc": "764503037108",
		"dealer_price": 449.0, "quantity": 5, "manufacturer": "Glock", "model": "19",
		"short_description": "GLOCK 19 GEN5", "department_number": "01", "ffl_required": 1}))
	stub.upsert("rsr", catalog_stub._normalize({"rsr_stock_number": "HSARO", "upc": "810047073659",
		"dealer_price": 171.54, "quantity": 213, "manufacturer": "Holosun", "model": "ARO",
		"short_description": "H-SUN ARO-EVO"}))
	stub.seed_restrictions("GLK19", [["", "CA", "state", "not approved"]])
	return stub


class TestReferencePasses(unittest.TestCase):
	def test_reference_stub_passes_every_check(self):
		results = check(StubTransport(_seeded()), "rsr", "GLK19")
		failed = [r for r in results if not r["ok"]]
		self.assertEqual(failed, [], "\n".join(f"{r['name']}: {r['detail']}" for r in failed))
		self.assertGreaterEqual(len(results), 14)

	def test_a_service_without_the_named_sku_stops_at_discovery(self):
		results = check(StubTransport(catalog_stub.CatalogStub()), "rsr", "GLK19")
		names = [r["name"] for r in results]
		self.assertIn("discovery", names)
		self.assertFalse(next(r for r in results if r["name"] == "discovery")["ok"])


class _Broken(StubTransport):
	"""A service whose search returns a best_offer that is not among offers,
	and whose unknown-UPC read answers 200."""

	def post(self, path, payload):
		st, body = super().post(path, payload)
		if path == "/v1/catalog/search" and body and body.get("results"):
			for hit in body["results"]:
				hit["best_offer"] = dict(hit["best_offer"], sku="SOMETHING-ELSE")
		return st, body

	def get(self, path, params=None):
		st, body = super().get(path, params)
		if path.startswith("/v1/catalog/item/") and st == 404:
			return 200, {"product": {}, "offers": [], "best_offer": None}
		return st, body


class TestViolationsAreCaught(unittest.TestCase):
	def test_best_offer_outside_offers_and_unknown_upc_200_fail(self):
		results = {r["name"]: r for r in check(_Broken(_seeded()), "rsr", "GLK19")}
		self.assertFalse(results["search.hit_shape"]["ok"])
		self.assertIn("best_offer not in offers", results["search.hit_shape"]["detail"])
		self.assertFalse(results["item.unknown_upc_404"]["ok"])
		# everything else still passes — one broken rule does not cascade
		self.assertTrue(results["rows.known_and_unknown"]["ok"])
		self.assertTrue(results["restrictions.known_shape"]["ok"])

	def test_an_absent_or_null_class_is_optional(self):
		class NullClass(StubTransport):
			def post(self, path, payload):
				st, body = super().post(path, payload)
				if path == "/v1/catalog/rows":
					body = {"rows": [dict(r, product_class=None) for r in body["rows"]]}
				return st, body

		results = {r["name"]: r for r in check(NullClass(_seeded()), "rsr", "GLK19")}
		self.assertTrue(results["rows.product_class"]["ok"])

	def test_the_operator_names_a_sku(self):
		from ffl_integrations.catalog_contract import run

		with self.assertRaisesRegex(ValueError, "Name a SKU"):
			run("https://catalog.example.com", "id", "secret", "rsr")

	def test_a_class_outside_the_list_fails(self):
		class Classless(StubTransport):
			def post(self, path, payload):
				st, body = super().post(path, payload)
				if path == "/v1/catalog/rows":
					body = {"rows": [dict(r, product_class="Pistol") for r in body["rows"]]}
				return st, body

		results = {r["name"]: r for r in check(Classless(_seeded()), "rsr", "GLK19")}
		self.assertFalse(results["rows.product_class"]["ok"])
