"""Transports for the conformance runner: the live HTTP service (signed like
the POS signs) and the in-memory reference stub."""
from __future__ import annotations

import json
from urllib.parse import quote


class HttpTransport:
	def __init__(self, base_url: str, client_id: str, secret: str, timeout: float = 15.0):
		self.base_url = base_url.rstrip("/")
		self.client_id, self.secret, self.timeout = client_id, secret, timeout

	def _headers(self, method: str, path: str, body: bytes) -> dict:
		from ffl_integrations.data_service.client import signed_headers
		return signed_headers(self.client_id, self.secret, method, path, body)

	def get(self, path: str, params: dict | None = None):
		import requests
		r = requests.get(self.base_url + path, params=params,
			headers=self._headers("GET", path, b""), timeout=self.timeout)
		return r.status_code, _json(r)

	def post(self, path: str, payload: dict):
		import requests
		body = json.dumps(payload, separators=(",", ":")).encode()
		r = requests.post(self.base_url + path, data=body,
			headers=self._headers("POST", path, body), timeout=self.timeout)
		return r.status_code, _json(r)


def _json(r):
	try:
		return r.json()
	except ValueError:
		return None


class StubTransport:
	"""Maps the five HTTP shapes onto tests/catalog_stub.CatalogStub — the
	reference implementation, so the contract runner can prove the reference
	and the document agree."""

	def __init__(self, stub):
		self.stub = stub

	def get(self, path: str, params: dict | None = None):
		p = params or {}
		if path == "/v1/catalog/health":
			h = dict(self.stub.health(p.get("distributor") or "rsr"))
			h.setdefault("contract_version", "1.0")
			return 200, h
		if path.startswith("/v1/catalog/item/"):
			hit = self.stub.item_by_upc(path.rsplit("/", 1)[1])
			if not hit:
				return 404, {"detail": "unknown upc"}
			offers = hit["offers"]
			best = min(offers, key=lambda o: (not (o.get("qty") or 0) > 0,
				(o.get("cost") or 0) <= 0, o.get("cost") or 0)) if offers else None
			return 200, {"product": hit["product"], "offers": offers, "best_offer": best}
		if path.startswith("/v1/catalog/restrictions/"):
			_, dist, sku = path.rsplit("/", 2)
			entries = self.stub.restrictions_entries(dist, sku)
			return 200, {"sku": sku, "entries": entries, "entry_count": len(entries)}
		return 404, None

	def post(self, path: str, payload: dict):
		if path == "/v1/catalog/search":
			hits = self.stub.search(payload.get("distributor"), payload["q"], int(payload.get("limit") or 10))
			return 200, {"results": hits}
		if path == "/v1/catalog/rows":
			rows = self.stub.rows(payload["distributor"], payload["skus"])
			return 200, {"rows": list(rows.values())}
		return 404, None


def item_path(upc: str) -> str:
	return "/v1/catalog/item/" + quote(str(upc), safe="")
