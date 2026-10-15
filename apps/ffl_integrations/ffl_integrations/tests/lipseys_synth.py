"""Synthetic Lipsey's API answers for the client / provider tests. Every value is
made up here: item numbers ZZL-nnnnn, 12-digit UPCs under a 99993 prefix, the
brand "Acme Test Arms", order numbers 7000nnn and invoice numbers 8000nnn. No
real Lipsey's item, UPC, title, price or account.

The API cannot be reached from development or CI (IP allowlist), so the shapes
follow Lipsey's published docs: the ``{success, authorized, errors, data}``
envelope, the login's ``{token, econtact}``, the order answers and the two catalog
feeds (CatalogFeed: a list of items; PricingQuantityFeed: ``{nextUpdate, items}``)."""
from __future__ import annotations

import json

import requests

BRAND = "Acme Test Arms"
TOKEN = "synthetic-token-not-a-real-one"
CUSTOMER = "900001"


def sku(n: int) -> str:
	return f"ZZL-{n:05d}"


def upc(n: int) -> str:
	return f"99993{n:07d}"


def envelope(data=None, *, success=True, authorized=True, errors=()) -> dict:
	return {"success": success, "authorized": authorized, "errors": list(errors), "data": data}


UNAUTHORIZED = envelope(None, success=False, authorized=False, errors=["Not Authorized"])


def login_ok(token: str = TOKEN, cus_no: str = CUSTOMER) -> dict:
	return {"token": token, "econtact": envelope({
		"name": "Test Dealer", "email": "dealer@example.com", "cusNo": cus_no,
		"shipTo": "1", "locationName": "Test Location", "slm": "0"})}


def login_refused() -> dict:
	return {"token": None, "econtact": envelope(None, success=False, authorized=False,
		errors=["Invalid email or password"])}


def validate(n: int, *, qty=5, price=101.5, blocked=False, allocated=False, can_dropship=True) -> dict:
	return envelope({"qty": qty, "price": price, "blocked": blocked, "allocated": allocated,
		"itemNumber": sku(n), "canDropship": can_dropship, "upc": upc(n),
		"mfgModelNumber": f"ATS-{n}", "standardPrice": price, "retailMap": 0, "onSale": False})


def catalog_item(n: int, **over) -> dict:
	"""One CatalogFeed item: a flagged semi-auto pistol unless ``over`` says otherwise."""
	item = {"itemNo": sku(n), "description1": f"ACME TEST PISTOL {n}", "description2": "9MM 4IN",
		"upc": int(upc(n)), "manufacturerModelNo": f"ATS-{n}", "msrp": 499.0, "model": f"AT{n}",
		"type": "Semi-Auto Pistol", "itemType": "Firearm", "manufacturer": BRAND,
		"price": 300.0 + n, "currentPrice": 300.0 + n, "retailMap": 0, "fflRequired": True,
		"sotRequired": False, "allocated": False, "quantity": 5, "canDropship": True,
		"caliberGauge": "9MM", "imageName": f"ZZL{n:05d}.jpg", "shippingWeight": 3.5}
	item.update(over)
	return item


def catalog_feed(items) -> dict:
	return envelope(list(items))


def pricing_item(n: int, *, qty=7, price=250.0, current=None, allocated=False) -> dict:
	return {"itemNumber": sku(n), "upc": int(upc(n)), "mfgModelNo": f"ATS-{n}", "quantity": qty,
		"allocated": allocated, "onSale": False, "price": price, "currentPrice": current,
		"retailMap": 0}


def pricing_feed(items, next_update="2026-01-01T01:00:00") -> dict:
	return envelope({"nextUpdate": next_update, "items": list(items)})


def order_line(item_no: str, *, requested=1, fulfilled=None, price=100.0, order="7000001",
		errors=(), order_error=False) -> dict:
	fulfilled = requested if fulfilled is None else fulfilled
	return {"itemNumber": item_no, "lipseysItemNumber": item_no, "note": "",
		"requestedQuantity": requested, "fulfilledQuantity": fulfilled, "errors": list(errors),
		"exists": True, "blocked": False, "allocated": False, "validForCart": True,
		"validForShipTo": True, "orderError": order_error, "orderNumber": order,
		"price": price, "totalPrice": round(price * fulfilled, 2)}


def api_order(*lines, success=True, errors=()) -> dict:
	"""APIOrder: ``data`` is the list of line results."""
	return envelope(list(lines), success=success, errors=errors)


def dropship_answer(*lines, order="7000101", hold=False, freight=12.5, tax=0.0,
		changes=(), success=True, errors=(), misc=None) -> dict:
	"""DropShip / DropShipFirearm: one order with lineItems and the totals."""
	data = {"lineItems": list(lines), "orderNumber": order, "accountHold": hold,
		"lineItemChanges": list(changes), "freight": freight, "tax": tax,
		"total": round(sum(l["totalPrice"] for l in lines) + freight + tax, 2)}
	if misc is not None:
		data["misc"] = misc
	return envelope(data, success=success, errors=errors)


def shipment(invoice: str, *, po="", order="7000001", tracking="1ZTEST0000000001",
		service="UPS Ground") -> dict:
	return {"invoiceNumber": invoice, "orderNumber": order, "poNumber": po,
		"billName": "Test Dealer", "shipName": "Test Dealer", "shippingService": service,
		"trackingNumber": tracking, "weight": "3"}


def one_day(*rows) -> dict:
	return envelope(list(rows))


class Resp:
	def __init__(self, payload, status=200, content_type="application/json; charset=utf-8"):
		self.status_code = status
		self._payload = payload
		self.headers = {"Content-Type": content_type}

	def json(self):
		if isinstance(self._payload, (bytes, str)):
			return json.loads(self._payload)
		return self._payload

	# a streamed GET: ``with session.get(..., stream=True) as resp``
	def __enter__(self):
		return self

	def __exit__(self, *exc):
		return False

	def body(self) -> bytes:
		if isinstance(self._payload, bytes):
			return self._payload
		if isinstance(self._payload, str):
			return self._payload.encode()
		return json.dumps(self._payload).encode()

	def iter_content(self, chunk_size=1):
		data = self.body()
		for start in range(0, len(data), chunk_size):
			yield data[start:start + chunk_size]


class FakeSession:
	"""``post(url, json=…, headers=…)`` → the scripted answer for the path's last
	two segments (``Order/APIOrder``). An answer is a dict (JSON 200), a ``Resp``,
	an exception instance (raised), a list (one per call, in turn) or a callable
	of the body."""

	def __init__(self, routes: dict):
		self.routes = {k: (list(v) if isinstance(v, list) else v) for k, v in routes.items()}
		self.calls = []
		self.closed = False

	def close(self):
		self.closed = True

	def get(self, url, headers=None, timeout=None, stream=False):
		return self._answer("GET", url, None, headers, timeout)

	def post(self, url, json=None, headers=None, timeout=None):
		return self._answer("POST", url, json, headers, timeout)

	def _answer(self, method, url, json, headers, timeout):
		path = "/".join(url.rsplit("/", 2)[-2:])
		self.calls.append({"method": method, "path": path, "body": json,
			"headers": dict(headers or {}), "timeout": timeout})
		answer = self.routes[path]
		if isinstance(answer, list):
			answer = answer.pop(0)
		if callable(answer) and not isinstance(answer, type):
			answer = answer(json)
		if isinstance(answer, BaseException):
			raise answer
		return answer if isinstance(answer, Resp) else Resp(answer)

	def paths(self) -> list:
		return [c["path"] for c in self.calls]


TIMEOUT = requests.ReadTimeout("read timed out")
CONNECT_TIMEOUT = requests.ConnectTimeout("connect timed out")
