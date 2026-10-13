"""RSR Direct Connect ordering API client.

Thin HTTP wrapper around RSR's Direct Connect POS bridge
(``{api_base_url}/api/rsrbridge/1.0/pos/*``). Every endpoint is a POST with a
JSON body; there is NO auth header — each call carries ``Username`` / ``Password``
/ ``POS`` (indicator) in the body. RSR issues **two independent credential
pairs**: a *dealer* account (stocking restock, procure-to-store) and a
*fulfillment* account (drop-ship, FDS transfers). The account is chosen per call
via the ``account`` argument.

Modelled on the FastBound / ShipStation clients: a ``RSRConfig`` dataclass read
from the RSR Settings single doctype, an injectable ``requests.Session`` (so
tests never touch the network), and a typed ``RSRError`` carrying the HTTP
``status_code`` plus a ``terminal`` flag that splits per-record failures (need a
human, never retry) from transient ones (reconcile re-drives).

Two RSR-specific behaviours live here:

* **Maintenance window** — RSR takes the ordering API down 3–5am America/New_York.
  place-order calls in that window are sent with ``Queue=Y`` so RSR queues rather
  than rejects them. The window check is confined to this module.
* **Idempotent placement** — a place-order that times out or 5xxes is ambiguous
  (the order may or may not have landed). ``place_order_idempotent`` NEVER blindly
  resubmits; it calls check-order on the PONum to disambiguate, adopting the order
  if RSR already has it. check-order itself falls back to check-combined-order
  when RSR reports StatusCode 98 or a combined signature (RSR Auto-Combine, H2).

Usage:
    from ffl_integrations.rsr.api_client import RSRClient
    client = RSRClient.from_settings()
    live = client.check_catalog(["GLK19GEN5"], account="dealer")
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import frappe
from ffl_core.credentials import RedactedRepr, Secret
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


SETTINGS_DOCTYPE = "RSR Settings"
# Same origin as RSR's website — the manual never names an API host, and the
# once-guessed api.rsrgroup.com is a DEAD name (no A record; a bogus AAAA
# pointing at Google DNS). Verified live 2026-08-02: unauthenticated POST to
# www's check-catalog answers with the rsrbridge envelope (StatusCode 96),
# and prod authenticated against both accounts right after the switch.
DEFAULT_API_BASE_URL = "https://www.rsrgroup.com"
DEFAULT_TIMEOUT = 30

# Fixed path prefix for every Direct Connect POS endpoint (Direct Connect manual, API base path).
API_PREFIX = "api/rsrbridge/1.0/pos"

# The 12 Direct Connect POS endpoints (all POST + JSON).
EP_CHECK_CATALOG = "check-catalog"
EP_PLACE_ORDER = "place-order"
EP_CHECK_ORDER = "check-order"
EP_CHECK_COMBINED_ORDER = "check-combined-order"
EP_GET_ITEMS = "get-items"
EP_GET_ITEM_ATTRIBUTES = "get-item-attributes"
EP_GET_DELETED_ITEMS = "get-deleted-items"
EP_GET_TRANSFER_DEALERS = "get-transfer-dealers"
EP_DEALER_ACCEPT_TRANSFERS = "dealer-accept-transfers"
EP_UPDATE_ORDER_FFL = "update-order-ffl"
EP_GET_SHIPMENTS = "get-shipments"
EP_GET_SHIPMENT = "get-shipment"

# RSR business-level StatusCode handling (response body, not HTTP status).
# Every request/response shape in this module was contract-verified against
# RSR's test environment (test.rsrgroup.com) on 2026-08-18.
# "live-verified" below refers to that sweep.
SUCCESS_STATUS_CODES = frozenset({"00", "0"})
# 98 = order not found under this PONum — frequently because RSR auto-combined it.
# Handled specially (RSROrderNotFound) so check-order can fall back to combined.
NOT_FOUND_STATUS_CODE = "98"
# place-order only: the order was accepted and queued for after the maintenance
# window (manual p.94 "60 – Order queued") — a success, not a fault. Manual-
# sourced, not part of the live sweep (no test order was placed in the window).
QUEUED_STATUS_CODE = "60"
# StatusCodes whose RSRError carries terminal=False. 87 = "Maintenance or fatal
# error. Try again later" (manual p.94/p.121) — the only code RSR itself marks
# transient; the old {"70"} placeholder matched nothing in any manual table.
# NOTE: today the flag is informational (place_order_job routes every failure
# to Error for a human) — nothing branches on .terminal from here yet.
RETRYABLE_STATUS_CODES = frozenset({"87"})
# TODO(go-live): still UNVERIFIED against live traffic — the test env produced
# no auto-combined order and no duplicate-PONum rejection, so _looks_combined's
# keys (Combined / CombinedPONum) and StatusCode 81 "Duplicate PO Num" remain
# manual-sourced assumptions. Confirm at first real integration (main-session).

# check-catalog real-time quote is capped at 100 SKUs per call (manual).
CHECK_CATALOG_MAX_SKUS = 100

# Deliberately-bogus probe SKU: authenticates the account with zero order-side
# effects (comes back per-item StatusCode 02 "Item not carried by RSR").
PROBE_SKU = "POS-PING"
# The fulfillment account geolocates a ship-to on EVERY check-catalog call
# (live-verified: omitting it → top-level 97 "Storename is required"), so the
# fulfillment probe carries a fixed real address — RSR's own Winter Park HQ,
# the address the manual's examples use. This EXACT probe shape (context +
# batch Items) was itself run against the test env (2026-08-18): clean
# geolocated response, per-item 02, no top-level StatusCode.
PROBE_SHIP_CONTEXT = {
	"Storename": "GunStore POS Connection Probe",
	"ShipAddress": "4405 Metric Dr",
	"ShipCity": "Winter Park",
	"ShipState": "FL",
	"ShipZip": "32792",
}

# RSR ordering-API maintenance window, America/New_York wall clock: [03:00, 05:00).
MAINTENANCE_TZ = "America/New_York"
MAINTENANCE_START_HOUR = 3
MAINTENANCE_END_HOUR = 5

Account = Literal["dealer", "fulfillment"]


class RSRError(RuntimeError):
	"""Raised on non-recoverable RSR API errors. Carries the HTTP ``status_code``
	(None for network failures) and the RSR business ``rsr_status`` so callers can
	branch on the code rather than substring-matching the message."""

	def __init__(self, *args, status_code: int | None = None, terminal: bool = False,
			rsr_status: str | None = None):
		super().__init__(*args)
		self.status_code = status_code
		# True when the failure is PER-RECORD terminal — needs a human and will never
		# self-heal on retry (bad SKU, invalid FFL, PONum reuse). NOT set for global/
		# config faults or transient errors (timeout, 5xx, maintenance), which must
		# STAY retryable so reconcile recovers them once the cause clears.
		self.terminal = terminal
		self.rsr_status = rsr_status


class RSRConfigError(RSRError):
	"""Settings missing/incomplete or the API disabled. Retryable — a config fault
	self-heals once corrected (mirrors FastBoundConfigError)."""


class RSROrderNotFound(RSRError):
	"""RSR StatusCode 98 — the PONum is not present under that number (commonly
	because the order was auto-combined). Signals check_order to fall back to
	check_combined_order (H2)."""


@dataclass(repr=False)
class RSRConfig(RedactedRepr):
	api_base_url: str
	pos_indicator: str
	dealer_account: str
	dealer_password: str
	fulfillment_account: str
	fulfillment_password: str
	api_enabled: bool
	# place-order's required Email field (manual: "Email of the dealer who
	# submitted the order", StatusCode 88 when missing) — the shop's, never the
	# consumer's or the transfer dealer's.
	order_email: str = ""
	timeout: int = DEFAULT_TIMEOUT

	@classmethod
	def from_settings(cls) -> "RSRConfig":
		"""Read the API region of the RSR Settings single doctype. Password fields
		go through Frappe's encryption (``get_password``)."""
		s = frappe.get_single(SETTINGS_DOCTYPE)
		base = (s.api_base_url or DEFAULT_API_BASE_URL).rstrip("/")
		dealer_password = s.get_password("dealer_password", raise_exception=False) or ""
		fulfillment_password = s.get_password("fulfillment_password", raise_exception=False) or ""
		return cls(
			api_base_url=base,
			pos_indicator=(s.pos_indicator or "").strip(),
			dealer_account=(s.dealer_account or "").strip(),
			dealer_password=dealer_password,
			fulfillment_account=(s.fulfillment_account or "").strip(),
			fulfillment_password=fulfillment_password,
			api_enabled=bool(s.api_enabled),
			order_email=(getattr(s, "order_email", "") or "").strip(),
			timeout=DEFAULT_TIMEOUT,
		)


def _status_message(data: dict) -> str:
	"""RSR's human-readable status text, wherever it hides: live traffic uses
	StatusMssg, the manual's examples show Message/StatusMessage. One reader so
	error raising and answer-classification (dealer_accept_transfers) can never
	disagree about where to look."""
	return str(data.get("Message") or data.get("StatusMessage")
		or data.get("StatusMssg") or "").strip()


def _extract_list(data: Any, *keys: str) -> list:
	"""First list found on ``data`` — the bare list itself, or the first of ``keys``
	whose value is a list. Tolerates RSR's enveloped ({StatusCode, Data:[...]}) and
	bare-list response shapes; returns [] when neither is present."""
	if isinstance(data, list):
		return data
	if isinstance(data, dict):
		for key in keys:
			value = data.get(key)
			if isinstance(value, list):
				return value
	return []


class RSRClient:
	def __init__(self, config: RSRConfig, session: requests.Session | None = None) -> None:
		self.config = config
		self.session = session or self._build_session()

	@classmethod
	def from_settings(cls) -> "RSRClient":
		return cls(RSRConfig.from_settings())

	# -------------------------------------------------- HTTP plumbing

	@staticmethod
	def _build_session() -> requests.Session:
		s = requests.Session()
		s.headers["Accept"] = "application/json"
		# Every RSR endpoint is a POST, several of which create/mutate an order. A
		# blind urllib3 retry could double-submit, so POST is excluded from the auto
		# retry policy — place_order_idempotent handles the ambiguous case explicitly.
		retry = Retry(
			total=3,
			backoff_factor=0.5,
			status_forcelist=(429, 500, 502, 503, 504),
			allowed_methods=frozenset(["GET", "HEAD", "OPTIONS"]),
		)
		adapter = HTTPAdapter(max_retries=retry)
		s.mount("https://", adapter)
		s.mount("http://", adapter)
		return s

	def _url(self, endpoint: str) -> str:
		return f"{self.config.api_base_url.rstrip('/')}/{API_PREFIX}/{endpoint.lstrip('/')}"

	def _now_et(self) -> datetime:
		"""Current America/New_York time. Isolated so tests can freeze the clock.

		Deliberately NOT the site's timezone and not the host's: the window below
		is RSR's own maintenance schedule, published in Eastern time, and it stays
		3–5am ET however the shop or the container is configured. Named explicitly
		(`datetime.now(ZoneInfo(...))`, never a bare `.now()`) so the tz sweep in
		`tests/test_timezone_family.py` reads it as the deliberate choice it is."""
		# tz-exempt: RSR's published window, not the shop's clock. Marked even
		# though the scanner already passes this line (it carries an argument), so
		# one grep for `tz-exempt:` lists every deliberate non-site clock we keep.
		return datetime.now(ZoneInfo(MAINTENANCE_TZ))

	def _in_maintenance_window(self, now_et: datetime | None = None) -> bool:
		"""True during RSR's 3–5am ET ordering-API maintenance window."""
		now_et = now_et or self._now_et()
		return MAINTENANCE_START_HOUR <= now_et.hour < MAINTENANCE_END_HOUR

	def _select_credentials(self, account: str) -> tuple[str, str]:
		if account == "dealer":
			acct, password = self.config.dealer_account, self.config.dealer_password
		elif account == "fulfillment":
			acct, password = self.config.fulfillment_account, self.config.fulfillment_password
		else:
			raise RSRError(
				f"Unknown RSR account '{account}' (expected 'dealer' or 'fulfillment').",
				terminal=True)
		if not acct or not password:
			raise RSRConfigError(
				f"RSR Settings: the {account} API account/password is not configured.")
		return acct, password

	def _post_raw(self, endpoint: str, body: dict | None, account: str) -> Any:
		"""Inject credentials + POS + (for place-order in the maintenance window)
		Queue=Y, then POST. Returns the parsed body WITHOUT inspecting the RSR
		StatusCode — callers that must not raise on a business error (e.g. the
		read-only connection probe) use this directly."""
		if not self.config.api_enabled:
			raise RSRConfigError("RSR API is disabled in RSR Settings (api_enabled is off).")
		username, password = self._select_credentials(account)
		payload = dict(body or {})
		payload["Username"] = username
		# Secret: every RSR call carries the password in its body, and this
		# dict is a local in the frames the Error Log prints (ffl_core.credentials).
		payload["Password"] = Secret(password)
		payload["POS"] = self.config.pos_indicator
		if endpoint == EP_PLACE_ORDER and self._in_maintenance_window():
			# 3–5am ET: RSR is in maintenance — queue the order instead of eating a
			# rejection. Queue is a place-order concept only, never a read call.
			payload["Queue"] = "Y"
		return self._request("POST", endpoint, json_body=payload)

	def _post(self, endpoint: str, body: dict | None, account: str) -> Any:
		data = self._post_raw(endpoint, body, account)
		self._raise_for_status_code(data, endpoint)
		return data

	def _request(self, method: str, endpoint: str, *, json_body: Any | None = None) -> Any:
		try:
			response = self.session.request(
				method,
				self._url(endpoint),
				json=json_body,
				timeout=self.config.timeout,
			)
		except requests.RequestException as exc:
			# Timeout/DNS/TLS failures surface as retryable RSRError (status_code None)
			# so the whitelisted layer turns an outage into a clean message and
			# reconcile re-drives it.
			raise RSRError(
				f"{method} {endpoint} → network error: {exc}",
				status_code=None, terminal=False) from exc
		if response.status_code >= 400:
			# 4xx = a definite client-side rejection (terminal); 5xx / 429 are transient
			# (retryable) — reconcile / idempotent placement recovers them.
			terminal = 400 <= response.status_code < 500 and response.status_code != 429
			raise RSRError(
				f"{method} {endpoint} → {response.status_code}: {response.text[:500]}",
				status_code=response.status_code, terminal=terminal)
		if not response.content:
			return None
		try:
			return response.json()
		except ValueError as exc:
			raise RSRError(
				f"{method} {endpoint} → non-JSON response: {response.text[:500]}",
				terminal=True) from exc

	def _raise_for_status_code(self, data: Any, endpoint: str) -> None:
		"""Translate a non-success RSR business StatusCode into an RSRError with the
		terminal split. A bare list / no StatusCode is treated as success."""
		if not isinstance(data, dict):
			return
		raw = data.get("StatusCode")
		if raw is None:
			return
		code = str(raw).strip()
		if code in SUCCESS_STATUS_CODES:
			return
		if code == QUEUED_STATUS_CODE and endpoint == EP_PLACE_ORDER:
			# 60 = order accepted, queued for after the maintenance window.
			# Only place-order defines it; anywhere else it stays an error.
			return
		message = _status_message(data)
		detail = f" {message}" if message else ""
		if code == NOT_FOUND_STATUS_CODE:
			raise RSROrderNotFound(
				f"{endpoint} → RSR order not found (StatusCode {code}).{detail}",
				terminal=True, rsr_status=code)
		terminal = code not in RETRYABLE_STATUS_CODES
		raise RSRError(
			f"{endpoint} → RSR StatusCode {code}.{detail}",
			terminal=terminal, rsr_status=code)

	# -------------------------------------------------- 12 endpoints

	def check_catalog(self, skus: list[str], account: Account = "dealer",
			ship_context: dict | None = None) -> list[dict]:
		"""Real-time quantity + price for up to 100 RSR stock #s. Used to
		second-confirm availability just before placing an order (buffer against
		feed staleness).

		ALWAYS the batch shape (LookupBy S + Items[]), even for one SKU: the
		single-item inputs (UPCcode/PartNum) answer in the topmost schema, where
		a per-item miss ("02 Item not carried") lands as the TOP-level StatusCode
		and _raise_for_status_code would turn "unknown item" into a raised error.
		The batch shape keeps per-item codes per item (live-verified).

		``ship_context`` is the fulfillment account's mandatory context: that
		account geolocates a ship-to on every call (97 without Storename), and
		firearms additionally require ShipFFL (84 without). Pass the manual's
		ship fields verbatim (Storename/ShipAddress/../ShipFFL/ContactNum)."""
		if len(skus) > CHECK_CATALOG_MAX_SKUS:
			raise RSRError(
				f"check-catalog accepts at most {CHECK_CATALOG_MAX_SKUS} SKUs, got {len(skus)}.",
				terminal=True)
		body: dict = {"LookupBy": "S", "Items": [str(s) for s in skus]}
		if ship_context:
			body.update(ship_context)
		data = self._post(EP_CHECK_CATALOG, body, account)
		return _extract_list(data, "Items", "Products", "Data")

	def place_order(self, payload: dict, account: Account) -> dict:
		"""Submit an order. Returns RSR's raw response (WebRef, totals, HoldStatus,
		line-level AckQty/Cost/ItemStatus) for the provider layer to normalise. In
		the maintenance window Queue=Y is added automatically. NOT auto-retried on
		timeout — use place_order_idempotent for the ambiguous case."""
		return self._post(EP_PLACE_ORDER, payload, account)

	def place_order_idempotent(self, po_num: str, payload: dict, account: Account) -> dict:
		"""Place an order, disambiguating an ambiguous failure via check-order rather
		than blindly resubmitting (which would risk a duplicate order at RSR).

		On a timeout or 5xx the order may or may not have landed, so we look it up by
		PONum: if RSR already has it, adopt it; if it's confirmed absent, raise a
		retryable error so the caller re-drives deliberately. A definite 4xx rejection
		is terminal — nothing landed, nothing to disambiguate."""
		body = dict(payload)
		body.setdefault("PONum", po_num)
		try:
			return self.place_order(body, account)
		except RSRError as exc:
			# Disambiguate ONLY on genuine transport uncertainty (timeout / 5xx),
			# where the order may or may not have landed. A business-layer rejection
			# always carries rsr_status (RSR responded with a StatusCode → the order
			# definitively did NOT land): re-raise it as-is. Routing a business
			# rejection into check-order would mask a terminal reason as retryable
			# and, on a reused PONum (StatusCode 81 "Duplicate PO Num"), could adopt a
			# prior unrelated order — the exact wrong-order failure this guards against.
			ambiguous = exc.rsr_status is None and (
				exc.status_code is None or exc.status_code >= 500)
			if not ambiguous:
				raise
			existing = self._find_existing_order(po_num, account)
			if existing is not None:
				return existing
			raise RSRError(
				f"place-order for PONum {po_num} is unconfirmed and not present at RSR; "
				f"not resubmitting.",
				status_code=exc.status_code, terminal=False) from exc

	def _find_existing_order(self, po_num: str, account: Account) -> dict | None:
		"""The RSR order for this PONum if it exists (direct or combined), else None."""
		try:
			status = self.check_order(po_num, account)
		except RSROrderNotFound:
			return None
		if isinstance(status, dict) and (
				status.get("PONum") or status.get("Items") or status.get("Lines")):
			return status
		return None

	def check_order(self, po_num: str, account: Account) -> dict:
		"""Line-level order status (Invoiced, Invoices[], DateShipped[], TrackingNum[],
		SerialNum[], HoldStatus). Falls back to check-combined-order when RSR reports
		StatusCode 98 (not found) or a combined signature — RSR Auto-Combine (H2).

		ReportAll=Y is mandatory plumbing, not an option: without it (or an
		Items[] echo of the order lines) RSR answers 96 "One of the following
		fields must be set: Items, ReportAll" (live-verified). We always want
		every line."""
		try:
			resp = self._post(EP_CHECK_ORDER, {"PONum": po_num, "ReportAll": "Y"}, account)
		except RSROrderNotFound:
			return self.check_combined_order(po_num, account)
		if self._looks_combined(resp):
			return self.check_combined_order(po_num, account)
		return resp

	def check_combined_order(self, po_num: str, account: Account) -> dict:
		"""Status for an order RSR merged under a combined PONum (H2 fallback target).
		Same mandatory ReportAll=Y as check-order (live-verified: same 96)."""
		return self._post(EP_CHECK_COMBINED_ORDER,
			{"PONum": po_num, "ReportAll": "Y"}, account)

	@staticmethod
	def _looks_combined(resp: Any) -> bool:
		if not isinstance(resp, dict):
			return False
		flag = str(resp.get("Combined", "")).strip().lower()
		return flag in {"y", "yes", "1", "true"} or bool(resp.get("CombinedPONum"))

	def get_items(self, filters: dict | None, account: Account) -> dict:
		"""Catalog SEARCH/dump — not a per-SKU lookup. get-items takes
		Keywords/Departments/Manufacturers/ModifiedSince/Limit/Offset/
		WithAttributes/... and pages the whole catalog; a PartNum or Items[]
		"filter" is silently ignored and the full catalog page comes back
		(live-verified: TotalFound echoed the entire book). Per-SKU reads are
		check_catalog (price/qty) or get_item_attributes (attributes).

		Returns the raw envelope (TotalFound/RowCount/Items[]) so callers can
		paginate on TotalFound."""
		return self._post(EP_GET_ITEMS, dict(filters or {}), account)

	def get_item_attributes(self, sku: str, account: Account) -> list[dict]:
		"""Structured attributes ([{Name, Value}, ...]) for ONE RSR stock #.
		Single-item only: the endpoint takes LookupBy S + PartNum and rejects an
		Items[] batch with 96 "PartNum is required" (live-verified)."""
		data = self._post(EP_GET_ITEM_ATTRIBUTES,
			{"LookupBy": "S", "PartNum": str(sku)}, account)
		return _extract_list(data, "Attributes", "Items", "Data")

	def get_deleted_items(self, account: Account) -> list[dict]:
		"""SKUs RSR has discontinued/removed. Response rows are LOWERCASE
		({"sku", "deleted_date"}, live-verified) and page at 9,999."""
		data = self._post(EP_GET_DELETED_ITEMS, {}, account)
		return _extract_list(data, "Items", "Deleted", "Data")

	def get_transfer_dealers(self, account: Account = "fulfillment") -> list[dict]:
		"""The COMPLETE transfer-dealer list (~15k rows of {Name, Street, City,
		State, Zip, FFL}). The endpoint has no server-side filter — a SearchZip
		param is silently ignored (live-verified) — so callers filter locally.
		Per-FFL verification is dealer_accept_transfers, not this list."""
		data = self._post(EP_GET_TRANSFER_DEALERS, {}, account)
		return _extract_list(data, "Dealers", "Data")

	def dealer_accept_transfers(self, ffl: str, account: Account = "fulfillment") -> dict:
		"""Per-FFL FDS pre-check: does RSR have this FFL on file, and does the
		holder accept transfers? Input is the FFL licence number (with or
		without dashes). An FFL RSR does not know answers StatusCode 96
		"FFL does not exist" — that is an ANSWER (not on file), not a fault, so
		it maps to on_file=False instead of raising. The 96-plus-message match
		is fragile by necessity: RSR reuses 96 for every validation error and
		gives this case no dedicated code."""
		data = self._post_raw(EP_DEALER_ACCEPT_TRANSFERS, {"FFL": ffl}, account) or {}
		code = str(data.get("StatusCode") or "").strip()
		if code == "96" and "not exist" in _status_message(data).lower():
			return {"FFL": ffl, "AcceptTransfers": "N", "on_file": False}
		self._raise_for_status_code(data, EP_DEALER_ACCEPT_TRANSFERS)
		data.setdefault("on_file", True)
		return data

	def update_order_ffl(self, web_ref: str, ffl: str, account: Account = "fulfillment") -> dict:
		"""Attach/replace the ShipFFL on a placed order to release an FDS Hold.
		Keyed by WebRef — the place-order confirmation number — NOT the PONum
		(live-verified: PONum-only → 96 "WebRef is required"). Success echoes
		the new dealer's on-file premises address."""
		return self._post(EP_UPDATE_ORDER_FFL,
			{"WebRef": str(web_ref), "ShipFFL": ffl}, account)

	def get_shipments(self, date_posted: str, account: Account) -> list:
		"""Invoice numbers for everything shipped on/after ``date_posted``
		(YYYY-MM-DD) — a date-range reconciliation feed, not a per-PO shipment
		list (live-verified: PONum → 96 "DatePosted required"). Feed each
		invoice number to get_shipment for detail."""
		data = self._post(EP_GET_SHIPMENTS, {"DatePosted": date_posted}, account)
		return _extract_list(data, "Invoices", "Data")

	def get_shipment(self, invoice_number: str, account: Account) -> dict:
		"""Detail (tracking, contents) for a single INVOICE from get_shipments —
		the endpoint's key is InvoiceNumber; there is no shipment-id concept."""
		return self._post(EP_GET_SHIPMENT,
			{"InvoiceNumber": str(invoice_number)}, account)

	def probe_connection(self, account: Account = "dealer") -> dict:
		"""Read-only credentials canary. Returns RSR's raw response even on a
		non-success StatusCode, so an auth problem surfaces as data to inspect
		rather than a raised error (a network/HTTP failure still raises).

		Shape matters: the batch Items[] form keeps the bogus probe SKU's "not
		carried" verdict per-item, so an authenticated account answers with NO
		top-level StatusCode at all (clean), a bad login answers 99, and the
		fulfillment account gets the fixed ship context it insists on. Callers
		classify: top-level StatusCode absent/00 ⇒ authenticated."""
		body: dict = {"LookupBy": "S", "Items": [PROBE_SKU]}
		if account == "fulfillment":
			body.update(PROBE_SHIP_CONTEXT)
		return self._post_raw(EP_CHECK_CATALOG, body, account) or {}
