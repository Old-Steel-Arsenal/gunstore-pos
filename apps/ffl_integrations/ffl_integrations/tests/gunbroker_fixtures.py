"""Real GunBroker responses, and a way to replay them through the real client.

Every body in this file was captured from the GunBroker sandbox on
2026-08-25/26 — with ONE exception,
the order bodies from :func:`order_detail` down, which are constructed from the
published field table because the sandbox account has never had an order on it.
That section says so again, at length, where it starts. Nothing else here is
invented, and that is the point of the file existing.

A mock that returns a dict tests almost nothing about this client. The parts
most likely to be wrong are the parts a dict skips straight past: deciding
whether a 404 is a missing listing or a bad URL (content-type, then JSON), and
turning `{"3": "Used"}` back into a condition code. So the helper below builds
a genuine `requests.Response` — status line, headers, raw bytes — and tests
patch `Session.request` to return it. The whole client runs, including the
parts that would otherwise only ever run in production.

The rule that follows from that: if a test needs a response shape nobody has
seen from GunBroker, it does not belong here. Add it once the sandbox has
produced it.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import requests

# --- POST /Items ------------------------------------------------------------
# The success body has NO itemID key. The number is inside prose, twice.
CREATE_LISTING_OK = {
	"userMessage": "Item listed with itemID: 16087230",
	"developerMessage": "Item listed with itemID: 16087230",
	"gbStatusCode": 0,
	"links": [{
		"rel": "self",
		"href": "https://api.sandbox.gunbroker.com/v1/items/16087230",
		"verb": "GET",
		"title": "16087230",
	}],
}

# Same call, with the prose changed and the links kept — the fallback path.
CREATE_LISTING_OK_LINKS_ONLY = {
	"userMessage": "Item listed.",
	"developerMessage": "Item listed.",
	"gbStatusCode": 0,
	"links": [{
		"rel": "self",
		"href": "https://api.sandbox.gunbroker.com/v1/items/16087231",
		"verb": "GET",
		"title": "16087231",
	}],
}

# A 200 whose body carries neither. Not observed from GunBroker; included as the
# shape the itemID parser must refuse to guess at.
CREATE_LISTING_OK_NO_ID = {
	"userMessage": "Item listed.", "gbStatusCode": 0, "links": [],
}

TITLE_POLICY_400 = {
	"userMessage": (
		"Item Title violates GunBroker.com listing policies. See Listing Policies "
		"and Best Practices (https://support.gunbroker.com/hc/en-us/articles/"
		"115000331433)"),
	"developerMessage": "Item Title violates GunBroker.com listing policies.",
	"gbStatusCode": 0,
	"links": [],
}

QUANTITY_ZERO_400 = {
	"userMessage": "The field Quantity must be an integer greater than 0.",
	"developerMessage": "The field Quantity must be an integer greater than 0.",
	"gbStatusCode": 0,
	"links": [],
}


# --- GET /Items/{id} --------------------------------------------------------
# Enum fields read back as ONE-ENTRY OBJECTS, not the integers that were sent.
def item_detail(item_id: str = "16087230", *, is_active: bool = True,
		fixed_price: float = 9999.99, sku: str = "PROBE::TEST0001",
		serial: str = "TEST0001", quantity: int = 1,
		has_winning_bid: bool = False, buyer=None, bids: int = 0,
		relisted_as: int = 0, is_fixed_price: bool = True,
		current_bid: float = 0.0) -> dict:
	return {
		"itemID": int(item_id),
		"categoryID": 3026,
		"categoryName": "Semi Auto Pistols",
		"condition": {"3": "Used"},
		"description": '<div id="Desc"><p>Probe listing</p></div>',
		"descriptionOnly": "<p>Probe listing</p>",
		"standardText": "",
		"eligibleForImmediateCheckout": False,
		"startingDate": "2026-08-25T23:34:23Z",
		"endingDate": "2026-09-24T23:34:23Z",
		"fixedPrice": fixed_price,
		"buyPrice": fixed_price,
		"hasPictures": True,
		"isActive": is_active,
		"isFFLRequired": True,
		"isFixedPrice": is_fixed_price,
		"currentBid": current_bid,
		"inspectionPeriod": {"1": "AS IS - No refund or exchange"},
		"location": "Dallas, TX 75206",
		"paymentMethods": {"8": "Visa / MasterCard"},
		"quantity": quantity,
		"relistedAsItemID": relisted_as,
		"salesTaxes": [],
		"serialNumber": serial,
		"shippingClassCosts": {"8": 40.0},
		"shippingClassesSupported": {"8": "Ground"},
		"sku": sku,
		"thumbnailURL": "https://res.cloudinary.com/gunbroker/pix_w_400,h_300,c_fit",
		"timeLeft": "P29DT23H59M41S",
		"title": "Probe listing for POS integration testing",
		"upc": "",
		"viewCounter": 0,
		"whoPaysForShipping": {"8": "Buyer pays fixed amount"},
		"canOffer": False,
		"collectsSalesTax": False,
		"hasWinningBid": has_winning_bid,
		"buyer": buyer,
		"bids": bids,
	}


# --- DELETE /Items ----------------------------------------------------------
def delete_ok(item_id: str = "16087239") -> dict:
	return {
		"userMessage": f"Item {item_id} is ended.",
		"developerMessage": f"Item {item_id} is ended.",
		"gbStatusCode": 0,
		"links": [],
	}


BATCH_DELETE_OK = {
	"successfullyEndedItems": [{"itemID": 16087238}, {"itemID": 16087237}],
	"gbStatusCode": 0,
	"links": [],
}

# GunBroker's answer before the DevKey was authorised for the operation. Kept
# because the client has to treat it as a permissions withdrawal, not a fault.
DELETE_FORBIDDEN_403 = {
	"userMessage": "You do not have permission to use this operation.",
	"developerMessage": "You do not have permission to use this operation.",
	"gbStatusCode": 0,
	"links": [],
}


# --- auth -------------------------------------------------------------------
LOGIN_OK = {"accessToken": "test-access-token-00000000000000"}

EXPIRED_TOKEN_401 = {
	"userMessage": "Expired or invalid X-AccessToken header value.",
	"developerMessage": "Expired or invalid X-AccessToken header value.",
	"gbStatusCode": 0,
	"links": [],
}


# --- 404, both kinds --------------------------------------------------------
# A missing RESOURCE: JSON, and evidence about a listing.
NOT_FOUND_JSON = {
	"userMessage": "Order 999999999 Not Found",
	"developerMessage": "Order 999999999 Not Found",
	"gbStatusCode": 0,
	"links": [],
}

# A missing ROUTE: an IIS page served through Cloudflare. Same status code, and
# it says nothing whatsoever about any listing.
NOT_FOUND_HTML = (
	"<!DOCTYPE html PUBLIC \"-//W3C//DTD XHTML 1.0 Strict//EN\">"
	"<html><head><title>404 - File or directory not found.</title></head>"
	"<body><h2>404 - File or directory not found.</h2></body></html>"
)

SERVER_ERROR_JSON = {
	"userMessage": "An error has occurred.",
	"developerMessage": "An error has occurred.",
	"gbStatusCode": 0,
	"links": [],
}


# --- listing scans ----------------------------------------------------------
def selling_row(item_id: int = 16087230, sku: str = "PROBE::TEST0001",
		serial: str = "TEST0001", *, bid_count: int = 0,
		is_fixed_price: bool = True, current_bid: float = 0.0) -> dict:
	"""One /ItemsSelling row. Note the two spellings of the ending time: the
	suffix-less one is Eastern and runs four hours behind its UTC twin."""
	return {
		"itemID": item_id,
		"sellerID": 1005379,
		"sku": sku,
		"serialNumber": serial,
		"title": "Probe listing for POS integration testing",
		"endingDateTime": "2026-09-24T19:30:42",
		"endingDateTimeUTC": "2026-09-24T23:30:42Z",
		"fixedPrice": 9999.99,
		"buyNowPrice": 9999.99,
		"quantity": 1,
		"bidCount": bid_count,
		"currentBid": current_bid,
		"isFixedPrice": is_fixed_price,
		"isFFLRequired": True,
	}


def page(rows: list, *, page_index: int = 1, page_size: int = 300) -> dict:
	return {
		"count": len(rows),
		"pageIndex": page_index,
		"pageSize": page_size,
		"gbStatusCode": 0,
		"results": rows,
		"links": [],
	}


# --- pictures ---------------------------------------------------------------
# A bare JSON string, not an object.
PICTURE_UPLOAD_OK = "Picture upload processed for item 16087232"

PICTURE_LIMIT_403 = {
	"userMessage": (
		"The number of files you are attempting to upload exceeds the limit of 8 "
		"per request."),
	"developerMessage": "The number of files you are attempting to upload exceeds the limit of 8 per request.",
	"gbStatusCode": 0,
	"links": [],
}


# --- account ----------------------------------------------------------------
GUNBROKER_TIME_OK = {
	"gunBrokerTime": "2026-08-26T18:57:06Z",
	"gunBrokerVersion": "1.0",
}

# The SHAPE the sandbox actually returns from `GET /Users/AccountInfo`, keys
# taken from a real response (2026-08-27) and values replaced.
#
# This used to be `{"userName": ..., "userID": ...}` — invented, flat, and
# nothing like the real thing. `test_connection` read `userName` off the top
# level, the fixture obligingly had one, every test passed, and the sandbox
# returned an empty account name forever. The top level carries the
# PERSON (firstName/lastName) and the contact details; the ACCOUNT lives one
# level down in `userSummary`. A fixture that cannot reproduce that distinction
# cannot catch a reader that gets it wrong.
ACCOUNT_INFO_OK = {
	"userSummary": {
		"userID": 1005379,
		"username": "store-sandbox",
		"accountStatus": 1,
	},
	"firstName": "Test",
	"lastName": "Seller",
	"address1": "1 Test Way",
	"publicEmailAddress": "seller@example.test",
	"registrationEmailAddress": "seller@example.test",
	"loginFlags": 0,
	"useBusiness": False,
	"fflSummary": {},
}

# Older/other GunBroker responses that the reader still has to cope with: a flat
# top-level `userName`, and an account with no username stated at all (where the
# only human-readable handle is the person's name).
ACCOUNT_INFO_FLAT = {"userName": "store-flat", "userID": 1005379}
ACCOUNT_INFO_NAME_ONLY = {"firstName": "Test", "lastName": "Seller"}

# The sandbox seller account has no payment gateway connected, which is also the
# intended production configuration for this phase (cheques and money orders).
LISTING_DEFAULTS_OK = {
	"gatewayEnabled": False,
	"checkoutEnabled": True,
	"salesTax": 2,
	"shipTerms": 4,
	"auctionLength": 7,
	"layawayAvailable": False,
	"standardTextID": None,
}


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def make_response(body, *, status: int = 200, content_type: str = "application/json",
		headers: dict | None = None) -> requests.Response:
	"""A genuine `requests.Response` carrying `body`.

	Built rather than mocked so the client's own parsing runs: `.headers`,
	`.content`, `.text` and `.json()` all behave the way they will in
	production, including `.json()` raising on the HTML 404 — which is precisely
	the branch that decides whether a listing is treated as gone.
	"""
	response = requests.Response()
	response.status_code = status
	payload = body if isinstance(body, (str, bytes)) else json.dumps(body)
	response._content = payload.encode("utf-8") if isinstance(payload, str) else payload
	response.headers["content-type"] = content_type
	for key, value in (headers or {}).items():
		response.headers[key] = value
	return response


def html_404() -> requests.Response:
	return make_response(NOT_FOUND_HTML, status=404, content_type="text/html")


def json_404(body=None) -> requests.Response:
	return make_response(body or NOT_FOUND_JSON, status=404)


class RecordingSession:
	"""A `requests.Session` stand-in that answers from a script and records calls.

	Answers are supplied as a list of `(matcher, response)` pairs where matcher
	is a `(method, path-fragment)` tuple; the first unused match wins, and a
	matcher may be reused by passing `repeat=True`.

	Deliberately NOT a bare `MagicMock` with a `side_effect` list. A positional
	script answers by call ORDER, so one extra read — a retry, a re-fetch — shifts
	every later answer and the test then asserts something nobody wrote. Matching
	on the request itself means a test fails when the client calls something
	unexpected, instead of quietly testing a different sequence.
	"""

	def __init__(self, script: list):
		self.script = list(script)
		self.calls: list[dict] = []
		self.headers: dict = {}

	def request(self, method, url, **kwargs):
		self.calls.append({
			"method": method, "url": url,
			"params": kwargs.get("params"),
			"json": kwargs.get("json"),
			"files": kwargs.get("files"),
			"headers": kwargs.get("headers") or {},
		})
		for entry in list(self.script):
			matcher, response = entry[0], entry[1]
			repeat = entry[2] if len(entry) > 2 else False
			want_method, want_fragment = matcher
			if method.upper() != want_method.upper() or want_fragment not in url:
				continue
			if not repeat:
				self.script.remove(entry)
			if callable(response):
				return response(method, url, kwargs)
			return response
		raise AssertionError(
			f"unscripted GunBroker call: {method} {url}\n"
			f"remaining script: {[e[0] for e in self.script]}")

	# --- assertions the tests read like sentences ---------------------------

	def urls_for(self, method: str) -> list[str]:
		return [c["url"] for c in self.calls if c["method"].upper() == method.upper()]

	def count(self, method: str, fragment: str = "") -> int:
		return len([c for c in self.calls
			if c["method"].upper() == method.upper() and fragment in c["url"]])

	def order_of(self, *fragments: str) -> list[int]:
		"""Index of the first call matching each fragment, for ordering asserts."""
		out = []
		for fragment in fragments:
			out.append(next(
				(i for i, c in enumerate(self.calls) if fragment in c["url"]), -1))
		return out


# The listing flags come from the implementation's own table, which
# test_gunbroker_field_contract reconciles against the DocType JSON. A second
# hand-copy here would only be able to disagree with it — and two copies that
# agree with each other while both drifting from the JSON is exactly the failure
# the reconciliation exists to catch.
from ffl_integrations.gunbroker.mapping import (  # noqa: E402
	LISTING_FLAG_DEFAULTS,
)


def flags_stub(**overrides) -> dict[str, bool]:
	"""Resolved listing flags, as `mapping.listing_flags()` would return them."""
	flags = {field: bool(default) for field, default in LISTING_FLAG_DEFAULTS.items()}
	flags.update({field: bool(value) for field, value in overrides.items()})
	return flags


def singles_rows(**stored) -> list[tuple[str, str]]:
	"""What `SELECT field, value FROM tabSingles` returns for one Single.

	Pass ONLY the fields that have actually been saved. Everything else is absent
	from the result — which is the real distinction a Single stores and the one
	thing a document read cannot recover, because loading a Single casts every
	Check through `cint()` and an unsaved checkbox arrives as 0.

	`singles_rows(sandbox_mode="1")` is the shape that matters: a site
	where a patch has written exactly one row, so every other field on the form
	reads back as 0 no matter what its default says.
	"""
	return [(field, str(value)) for field, value in stored.items()]


def singles_sql(**stored):
	"""A `frappe.db.sql` side effect that answers both tabSingles queries.

	`single_raw` asks for one field and reads `rows[0][0]`; `single_raw_map` asks
	for all of them and unpacks `(field, value)` pairs. A single canned return
	value cannot satisfy both, and getting it wrong is silent — `single_raw`
	would happily `cint()` a field NAME and read the switch as off.

	Pass only the fields that have been saved, as in :func:`singles_rows`.
	"""
	rows = {field: str(value) for field, value in stored.items()}

	def _sql(query, values=None, *args, **kwargs):
		if "`field` = " in query:
			field = values[1] if isinstance(values, (tuple, list)) else None
			return [(rows[field],)] if field in rows else []
		return [(field, value) for field, value in rows.items()]

	return _sql


def settings_stub(**overrides) -> MagicMock:
	"""A GunBroker Settings double with this phase's shipped defaults."""
	values = {
		"enabled": 1,
		"username": "seller",
		"sandbox_mode": 1,
		"base_url_override": "",
		"request_timeout_seconds": 30,
		"probe_timeout_seconds": 3,
		"page_size": 300,
		"end_strategy": "Delete Endpoint",
		"max_pictures": 16,
		"postal_code": "75206",
		"country_code": "US",
		"listing_duration": "30",
		"auto_relist": "4 Relist Fixed Price",
		"default_listing_type": "Fixed Price",
		"auction_duration": "7",
		"auction_auto_relist": "2 Relist Until Sold",
		"is_ffl_required": 1,
		"will_ship_international": 0,
		"who_pays_shipping": "8 Buyer pays fixed",
		"long_gun_ship_cost": 45.00,
		"handgun_ship_cost": 34.99,
		"accessory_ship_cost": 19.99,
		"inspection_period": "1 AS IS",
		"use_default_exclude_states": 1,
		"exclude_states": "",
		"pay_certified_check": 1,
		"pay_usps_money_order": 1,
		"pay_money_order": 1,
		"card_checkout_enabled": 0,
		"pay_visa_mastercard": 0,
		"pay_amex": 0,
		"pay_discover": 0,
		"pay_personal_check": 0,
		"pay_cod": 0,
		"title_banned_phrases": "do not buy",
		"description_footer": "",
		"category_map": [],
		"last_listing_push_count": 0,
	}
	values.update(overrides)

	stub = MagicMock()

	def _get(key, default=None):
		value = values.get(key, default)
		# Mirror Frappe: loading a Single runs every Check through cint(), so a
		# checkbox NEVER reads back as None off a document — it is 0 or 1. A stub
		# that hands back None would let a test pass against a state Frappe cannot
		# produce, which is exactly how the first attempt at this went wrong.
		if key in LISTING_FLAG_DEFAULTS and value is None:
			return 0
		return value

	stub.get.side_effect = _get
	stub.get_password.side_effect = lambda field, **_: {
		"password": "secret",
	}.get(field, "")
	for key, value in values.items():
		setattr(stub, key, value)
	return stub


def dev_keys(production: str = "prod-key", sandbox: str = "sandbox-key"):
	"""The platform DevKeys as site config carries them — a patcher.

	The Settings form no longer holds them, so a test that wants a configured
	store patches ``frappe.conf`` rather than ``settings.get_password``. Use as
	a context manager, or ``start()``/``stop()`` it from a harness.
	"""
	import frappe

	return patch.dict(frappe.local.conf, {
		"gunbroker_dev_key": production, "gunbroker_sandbox_dev_key": sandbox})


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
#
# The sandbox seller account has never had an order on it, and QA cannot make
# one (a second buyer account would be needed, and GunBroker does not hand them
# out). So unlike everything above, these bodies are built from the published
# field tables rather than captured — and that is written here rather than
# quietly assumed, because it decides what a green test proves.
#
# Consequences, both deliberate:
#   * every key is spelled exactly as the API reference spells it, camelCase and
#     all, including the ones that differ between the list row and the detail;
#   * fields the reference lists but nobody has ever seen a value for are
#     PRESENT with plausible values rather than omitted, so the mapper is
#     exercised on the shape it will actually meet;
#   * nothing here proves GunBroker really sends this. The first production
#     order is checked by hand against it.

def orders_sold_row(order_id: str = "998877", *, modified: str = "2026-08-26T14:00:00Z",
		status: int = 4, total: float = 1245.99, ffl_received: bool = True,
		payment_received: bool = False) -> dict:
	"""One ``GET /OrdersSold`` row. Note what is NOT here: no line collection,
	so no serial number and no SKU — which is why the poller always fetches the
	order itself before it can match anything to a gun."""
	return {
		"orderID": int(order_id),
		"orderDateUTC": "2026-08-26T13:05:00Z",
		"lastModifiedDate": modified,
		"orderStatus": status,
		"totalPrice": total,
		"paymentReceived": payment_received,
		"fflReceived": ffl_received,
		"isChargeback": False,
		"buyer": {"userID": 55501, "username": "collector_dave", "isFFL": False},
		"billToName": "Dave Collector",
		"billToEmail": "dave@example.com",
		"itemIDs": [16087230],
	}


def order_detail(order_id: str = "998877", *, status: int = 4,
		sku: str = "COLT-PYTHON::SN-001", serial: str = "SN-001",
		item_price: float = 1100.00, ship_cost: float = 45.00,
		sales_tax_total: float = 100.99, compliance_fee: float = 11.00,
		total: float = 1245.99, ffl_number: str = "1-99-888-01-2A-34567",
		ffl_received: bool = True, payment_received: bool = False,
		tracking: str = "", carrier: int = 0, extra_items: list | None = None,
		**overrides) -> dict:
	"""``GET /Orders/{orderID}`` — the only place a line names its gun.

	``salesTaxTotal`` is GunBroker's combined figure and ``complianceFee`` is
	the buyer's 1% inside it; the defaults here are consistent with each other
	(11.00 is 1% of 1100.00) so a test that splits them gets a believable pair.
	"""
	items = [{
		"itemID": 16087230,
		"title": "Colt Python 357 Magnum",
		"quantity": 1,
		"itemPrice": item_price,
		"itemSubTotal": item_price,
		"serialNumber": serial,
		"sku": sku,
		"isFFLRequired": True,
		"salesTax": sales_tax_total - compliance_fee,
		"itemCondition": "Used",
	}]
	items.extend(extra_items or [])
	body = {
		"orderID": int(order_id),
		"orderDateUTC": "2026-08-26T13:05:00Z",
		"lastModifiedDate": "2026-08-26T14:00:00Z",
		"status": status,
		"orderItemsCollection": items,
		"itemIDs": [i["itemID"] for i in items],
		"buyer": {"userID": 55501, "username": "collector_dave", "isFFL": False},
		"billToName": "Dave Collector",
		"billToEmail": "dave@example.com",
		"billToPhone": "214-555-0100",
		"shipToName": "Far End Guns",
		"shipToAddress1": "900 Dealer Row",
		"shipToAddress2": "",
		"shipToCity": "Austin",
		"shipToState": "TX",
		"shipToPostalCode": "78701",
		"fflNumber": ffl_number,
		"fflReceived": ffl_received,
		"fflReceivedDateUTC": "2026-08-26T13:30:00Z" if ffl_received else None,
		"fflStatus": "Received" if ffl_received else "Awaiting",
		"eFFLUrl": "https://www.gunbroker.com/effl/998877",
		"sellerPacketURL": "https://www.gunbroker.com/packet/998877",
		"paymentReceived": payment_received,
		"paymentReceivedDateUTC": "2026-08-26T13:20:00Z" if payment_received else None,
		# The enum object form, as every other GunBroker enumeration reads back.
		"paymentMethods": {"8192": "Certified Check"},
		"totalPrice": total,
		"shipCost": ship_cost,
		"shipHandlingCost": 0,
		"shipInsuranceCost": 0,
		"salesTaxTotal": sales_tax_total,
		"complianceFee": compliance_fee,
		"buyersPremium": 0,
		"couponValue": 0,
		"lostCashDiscount": 0,
		"miscCharge": 0,
		"miscFee": 0,
		"taxExempt": False,
		"isChargeback": False,
		"shipTrackingNumber": tracking,
		"shipCarrier": carrier,
	}
	body.update(overrides)
	return body


def order_item(item_id: int, *, sku: str, serial: str, price: float,
		ffl_required: bool = True, tax: float = 0.0) -> dict:
	"""A second (or third) line for order_detail(extra_items=[...])."""
	return {
		"itemID": item_id, "title": f"Item {item_id}", "quantity": 1,
		"itemPrice": price, "itemSubTotal": price, "serialNumber": serial,
		"sku": sku, "isFFLRequired": ffl_required, "salesTax": tax,
		"itemCondition": "Used",
	}


# ---------------------------------------------------------------------------
# A document that behaves like a Frappe one without a database
# ---------------------------------------------------------------------------

class FakeChildRow(dict):
	"""A child-table row: attribute access, and db_set writes to itself.

	db_set writing only to memory is right for these tests — the parent is
	saved at the end of every path that uses it, so what the row holds IS what
	would be persisted, and a test asserting on it is asserting the same thing
	the database would.
	"""

	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError:
			return None

	def __setattr__(self, key, value):
		self[key] = value

	def get(self, key, default=None):
		return dict.get(self, key, default)

	def db_set(self, field, value=None, **_kw):
		if isinstance(field, dict):
			self.update(field)
		else:
			self[field] = value


class FakeDoc(dict):
	"""Enough of a Document for the order mapper and the fulfillment gates.

	Records saves and comments so a test can assert the order was written, and
	keeps child tables as plain lists of FakeChildRow.
	"""

	def __init__(self, doctype: str = "GunBroker Order", **fields):
		super().__init__()
		self["doctype"] = doctype
		self["name"] = fields.pop("name", "GB-ORD-00001")
		self.update(fields)
		self.saves = 0
		self.comments: list[str] = []
		self.flags = _Flags()

	def __getattr__(self, key):
		if key in ("saves", "comments", "flags"):
			raise AttributeError(key)
		try:
			return self[key]
		except KeyError:
			return None

	def __setattr__(self, key, value):
		if key in ("saves", "comments", "flags"):
			object.__setattr__(self, key, value)
		else:
			self[key] = value

	def get(self, key, default=None):
		return dict.get(self, key, default)

	def set(self, key, value):
		self[key] = value

	def append(self, table, row):
		child = FakeChildRow(row)
		self.setdefault(table, [])
		self[table].append(child)
		return child

	def save(self, *_a, **_kw):
		self.saves += 1
		return self

	def insert(self, *_a, **_kw):
		self.saves += 1
		return self

	def submit(self, *_a, **_kw):
		self["docstatus"] = 1
		return self

	def reload(self):
		return self

	def add_comment(self, _kind, text):
		self.comments.append(text)

	def db_set(self, field, value=None, **_kw):
		if isinstance(field, dict):
			self.update(field)
		else:
			self[field] = value


class _Flags:
	def __init__(self):
		self.ignore_permissions = False
