"""The checks. Each returns ``{"name", "ok", "detail"}``; ``check`` never
raises on a service fault — a broken endpoint is a failed check, reported
with what came back."""
from __future__ import annotations

from datetime import datetime

from ffl_integrations.distributor.util import PRODUCT_CLASSES  # a full row's optional class ("" = none)

from .transports import HttpTransport, item_path

OFFER_KEYS = {"distributor": str, "sku": str, "manufacturer": str, "model": str,
	"department": str, "cost": (int, float), "map": (int, float), "msrp": (int, float),
	"qty": int, "ffl_required": bool, "dropship_blocked": bool,
	"vendor_approval_required": bool, "image_url": str, "discontinued": bool,
	"status_flag": str, "short_description": str}
OFFER_FULL_KEYS = {**OFFER_KEYS, "mfg_number": str, "caliber": str, "long_description": str,
	"image_name": str, "image_url_hires": str, "weight_lb": (int, float), "prop65": bool,
	"ships_ground_only": bool, "signature_required": bool}
HEALTH_TS = ("last_catalog_sync_at", "last_quantity_sync_at", "last_restrictions_sync_at")


def _typed(obj: dict, spec: dict) -> list[str]:
	bad = []
	for k, t in spec.items():
		if k not in obj:
			bad.append(f"missing {k}")
		elif obj[k] is not None and not isinstance(obj[k], t) or (t is int and isinstance(obj[k], bool)):
			bad.append(f"{k} is {type(obj[k]).__name__}")
	return bad


def _offer_ok(o: dict, full: bool = False) -> list[str]:
	bad = _typed(o, OFFER_FULL_KEYS if full else OFFER_KEYS)
	if o.get("upc") is not None and not isinstance(o.get("upc"), str):
		bad.append("upc must be string or null")
	if isinstance(o.get("upc"), str) and o["upc"] != o["upc"].strip():
		bad.append("upc carries whitespace")
	return bad


def _ts_ok(v) -> bool:
	if v is None:
		return True
	try:
		d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
	except ValueError:
		return False
	return d.tzinfo is not None


def _same(a: dict, b: dict) -> bool:
	return (a.get("distributor"), a.get("sku")) == (b.get("distributor"), b.get("sku"))


def check(transport, distributor: str, sku: str) -> list[dict]:
	out: list[dict] = []

	def rec(name, ok, detail=""):
		out.append({"name": name, "ok": bool(ok), "detail": detail})
		return ok

	# ---- health
	st, h = transport.get("/v1/catalog/health", {"distributor": distributor})
	if not rec("health.reachable", st == 200 and isinstance(h, dict), f"HTTP {st}"):
		return out
	bad = _typed(h, {"distributor": str, "catalog_version": str,
		"offers_total": int, "offers_active": int})
	bad += [f"{k} not ISO-with-offset" for k in HEALTH_TS if not _ts_ok(h.get(k))]
	rec("health.shape", not bad and h.get("distributor") == distributor, "; ".join(bad))
	cv = str(h.get("contract_version") or "")
	rec("health.contract_version", cv.startswith("1."),
		f"got {cv!r} (1.x expected; missing = pre-1.0 service)")

	# ---- rows (the operator names a SKU the service carries)
	st, r = transport.post("/v1/catalog/rows", {"distributor": distributor, "skus": [sku, "NO-SUCH-SKU-X"]})
	got = (r or {}).get("rows") if st == 200 else None
	if not got:
		rec("discovery", False, f"HTTP {st}: {sku!r} is not on /rows — remaining checks need one product")
		return out
	ok = [x["sku"] for x in got] == [sku]
	rec("rows.known_and_unknown", ok, f"HTTP {st}, rows={[x.get('sku') for x in got]}")
	known = got[0]
	bad = _offer_ok(known, full=True)
	rec("rows.full_shape", not bad and known.get("distributor") == distributor, "; ".join(bad))
	klass = known.get("product_class") or ""
	rec("rows.product_class", klass in ("", *PRODUCT_CLASSES),
		f"got {klass!r} (optional; one of {', '.join(PRODUCT_CLASSES)})")
	upc = known.get("upc")

	# ---- search
	st, s = transport.post("/v1/catalog/search", {"q": sku, "limit": 5})
	hits = (s or {}).get("results") if st == 200 else None
	rec("search.unscoped", isinstance(hits, list) and len(hits) <= 5, f"HTTP {st}")
	shape_bad = []
	for hit in hits or []:
		offers = hit.get("offers") or []
		if not offers:
			shape_bad.append("hit without offers")
		for o in offers:
			shape_bad += _offer_ok(o)
		best = hit.get("best_offer")
		if best is not None and not any(_same(best, o) for o in offers):
			shape_bad.append("best_offer not in offers")
		if hit.get("upc") is not None and not isinstance(hit["upc"], str):
			shape_bad.append("hit.upc must be string or null")
	rec("search.hit_shape", not shape_bad, "; ".join(sorted(set(shape_bad)))[:300])
	rec("search.finds_known_sku", any(any(o.get("sku") == sku and o.get("distributor") == distributor
		for o in (hit.get("offers") or [])) for hit in (hits or [])), "known sku absent from its own search")
	st, s = transport.post("/v1/catalog/search", {"q": sku, "limit": 5, "distributor": distributor})
	hits = (s or {}).get("results") if st == 200 else []
	rec("search.scoped", st == 200 and all(o.get("distributor") == distributor
		for hit in hits for o in (hit.get("offers") or [])), f"HTTP {st}")
	st, s = transport.post("/v1/catalog/search", {"q": sku, "limit": 1})
	rec("search.limit", st == 200 and len((s or {}).get("results") or []) <= 1, f"HTTP {st}")

	# ---- item
	if upc:
		st, it = transport.get(item_path(upc))
		ok = st == 200 and isinstance((it or {}).get("offers"), list) and isinstance((it or {}).get("product"), dict)
		rec("item.known_upc", ok, f"HTTP {st}")
		if ok:
			rec("item.has_our_offer", any(_same(o, {"distributor": distributor, "sku": sku})
				for o in it["offers"]), "the known row is missing from its UPC's offers")
			best = it.get("best_offer")
			rec("item.best_offer_in_offers", best is None or any(_same(best, o) for o in it["offers"]))
	else:
		rec("item.known_upc", True, "skipped: discovered row has no upc")
	st, _ = transport.get(item_path("000000000000-NOPE"))
	rec("item.unknown_upc_404", st == 404, f"HTTP {st}")

	# ---- restrictions
	st, rs = transport.get(f"/v1/catalog/restrictions/{distributor}/NO-SUCH-SKU-X")
	rec("restrictions.unknown_is_empty_200", st == 200 and (rs or {}).get("entries") == []
		and (rs or {}).get("entry_count") == 0, f"HTTP {st}")
	st, rs = transport.get(f"/v1/catalog/restrictions/{distributor}/{sku}")
	rec("restrictions.known_shape", st == 200 and isinstance((rs or {}).get("entries"), list)
		and all(isinstance(e, list) and len(e) == 4 for e in rs["entries"]), f"HTTP {st}")
	return out


def run(base_url: str, client_id: str, secret: str, distributor: str = "rsr",
		sku: str = "") -> dict:
	"""Operator entry point:
	bench --site x execute ffl_integrations.catalog_contract.run --kwargs '{...}'
	``sku``: one the service carries for ``distributor`` (the checks need a product)."""
	if not sku:
		raise ValueError("Name a SKU the service carries (sku=...): the checks read it.")
	results = check(HttpTransport(base_url, client_id, secret), distributor, sku)
	failed = [r for r in results if not r["ok"]]
	for r in results:
		# detail is the FAILURE explanation; a passing check prints only its name
		print(("PASS " if r["ok"] else "FAIL ") + r["name"]
			+ (f" — {r['detail']}" if (r["detail"] and not r["ok"]) else ""))
	print(f"{len(results) - len(failed)}/{len(results)} checks passed")
	return {"passed": len(results) - len(failed), "failed": len(failed), "results": results}
