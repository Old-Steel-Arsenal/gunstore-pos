"""Sports South ordering client — orders.asmx + invoices.asmx (SMART web
services), HTTP POST binding, form-urlencoded, XML-in-a-string envelope.

Wire facts (from the vendor WSDL):

* Every response is ``<string>…escaped XML…</string>`` (or a bare scalar for
  the int/boolean methods). Unescape once, then parse.
* **Auth failures are shape-dependent and partly SILENT**: dataset methods
  answer ``NewDataSet/Table1/ERROR``; GetDetail answers a bare
  ``<ERROR>USER NOT AUTHENTICATED!</ERROR>``; FFLAcceptsTransfer answers the
  literal ``AUTHFAIL``; GetCreditsByDate answers the literal
  ``System.Data.DataSet``; **ShipCheck / GetEstimatedShipCost answer EMPTY**
  on bad credentials — an empty ShipCheck is only "compliant" after a loud
  method has proven the credentials in the same session (``assert_auth``).
* AddHeader returns the SS web order number (``0`` = failure — the WSDL
  documents 0, the PDF says "an error describing why"; both handled);
  AddDetail / Submit / DeleteOpenOrder return ``true``/``false`` with NO
  reason. OrderPrice on AddDetail is a note, not a price lock (official
  sample: sent 1.00, charged 19.50) — reconcile ORPRCF after Submit.
* Dates: GetCreditsByDate wants ``YYYYMMDD``; invoices.asmx timestamps carry
  a REAL ``-05:00`` offset (inventory.asmx's are fake ``+00:00``). Formats are
  pinned per method here; never share a formatter with the catalog side.

Pure transport + parsing: ``frappe`` is imported only inside
``SSConfig.from_settings``. Never touch inventory.asmx from here — the
catalog lives in the Data Service.
"""
from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from ffl_core.credentials import RedactedRepr, Secret
from typing import Any

import requests

log = logging.getLogger(__name__)

SETTINGS_DOCTYPE = "Sports South Settings"
DEFAULT_BASE_URL = "https://webservices.theshootingwarehouse.com/smart"
DEFAULT_TIMEOUT = 30
# The SS-published universal test customer. With OUR credentials it is only
# ever sent as ``Source`` (sandbox routing per SS help desk); the test
# customer's own login does not authenticate against orders/invoices (probed
# 2026-09-08).
SANDBOX_SOURCE = "99994"

ORDERS = "orders"
INVOICES = "invoices"

# Literal bodies SS returns instead of XML when authentication fails on the
# string-returning methods. Any of these anywhere in a body = auth failure.
_AUTH_LITERALS = ("AUTHFAIL", "System.Data.DataSet", "USER NOT AUTHENTICATED",
	"Authentication Failed For User")


class SSError(RuntimeError):
	"""Non-recoverable SS API error. ``terminal`` = needs a human, will not
	self-heal on retry (a refused line, an unknown item); transient failures
	(network, 5xx) stay retryable so the queue re-drives them."""

	def __init__(self, *args, status_code: int | None = None, terminal: bool = False):
		super().__init__(*args)
		self.status_code = status_code
		self.terminal = terminal


class SSConfigError(SSError):
	"""Settings missing / ordering disabled. Retryable — self-heals once fixed."""


class SSAuthError(SSError):
	"""Credentials rejected. Terminal for the request, config-level for the shop."""


@dataclass(repr=False)
class SSConfig(RedactedRepr):
	customer_number: str
	username: str
	password: str
	source: str
	base_url: str = DEFAULT_BASE_URL
	ordering_enabled: bool = False
	timeout: int = DEFAULT_TIMEOUT

	@classmethod
	def from_settings(cls) -> "SSConfig":
		import frappe

		s = frappe.get_single(SETTINGS_DOCTYPE)
		customer = (s.customer_number or "").strip()
		# Source = our own account number on live (SS onboarding: "Source: your
		# account number"); the sandbox flag swaps it for 99994.
		source = SANDBOX_SOURCE if s.sandbox_mode else customer
		return cls(
			customer_number=customer,
			username=(s.username or "").strip() or customer,
			password=s.get_password("password", raise_exception=False) or "",
			source=source,
			base_url=(s.base_url or DEFAULT_BASE_URL).rstrip("/"),
			ordering_enabled=bool(s.ordering_enabled),
			timeout=DEFAULT_TIMEOUT,
		)


# ------------------------------------------------------------------ parsing

_STRING_RE = re.compile(r"^\s*(?:<\?xml[^>]*\?>)?\s*<(string|int|boolean)\b[^>]*>(.*)</\1>\s*$", re.S)
_EMPTY_RE = re.compile(r"^\s*(?:<\?xml[^>]*\?>)?\s*<(string|int|boolean)\b[^>]*/>\s*$", re.S)


def unwrap(body: str) -> tuple[str, str]:
	"""``(kind, inner)`` from an asmx body: kind ∈ {string, int, boolean, raw};
	``inner`` is the unescaped payload (XML text for string, scalar otherwise)."""
	m = _STRING_RE.match(body or "")
	if not m:
		e = _EMPTY_RE.match(body or "")
		if e:
			return e.group(1), ""       # self-closing = empty payload
		return "raw", (body or "").strip()
	kind, inner = m.group(1), m.group(2)
	if kind == "string":
		inner = html.unescape(inner)
	return kind, inner.strip()


def sniff_error(inner: str) -> str | None:
	"""The ERROR text if this payload is an SS-side error, else None. Covers the
	dataset shape (``Table1/ERROR``), the bare ``<ERROR>`` root and the literal
	auth strings."""
	text = (inner or "").strip()
	if not text:
		return None
	for lit in _AUTH_LITERALS:
		if lit in text and "<" not in text[:1]:
			return text[:200]
	if text.startswith("<"):
		try:
			root = ET.fromstring(text)
		except ET.ParseError:
			return None
		if root.tag == "ERROR":
			return (root.text or "").strip() or "ERROR"
		err = root.find(".//ERROR")
		if err is not None:
			return (err.text or "").strip() or "ERROR"
	return None


def is_auth_error(message: str | None) -> bool:
	msg = (message or "").upper()
	return any(lit.upper() in msg for lit in _AUTH_LITERALS)


def parse_records(inner: str, tags=("Table", "Table1", "Header", "Detail")) -> list[dict]:
	"""Flat ``[{tag, field: text}]`` for every record element under the dataset
	root. ``<NewDataSet />`` → []. Records keep their element tag under ``_tag``
	so credit memos (Header + Detail) stay distinguishable."""
	text = (inner or "").strip()
	if not text or not text.startswith("<"):
		return []
	root = ET.fromstring(text)
	out = []
	for el in root:
		if el.tag in tags:
			rec = {c.tag: (c.text or "").strip() for c in el}
			rec["_tag"] = el.tag
			out.append(rec)
	return out


def _to_int(v, default=0) -> int:
	try:
		return int(float(str(v).strip()))
	except (TypeError, ValueError):
		return default


def _to_float(v, default=0.0) -> float:
	try:
		return float(str(v).replace("$", "").replace(",", "").strip())
	except (TypeError, ValueError):
		return default


def normalize_detail_rows(records: list[dict]) -> list[dict]:
	"""GetDetail rows → ``[{sku, ordered_qty, filled_qty, order_price,
	filled_price, customer_item, customer_desc}]``. ORQTYF < ORQTY is a short
	(0 = fully shorted); ORPRCF is what SS will actually charge."""
	rows = []
	for r in records:
		if "ORITEM" not in r:
			continue
		rows.append({
			"sku": r.get("ORITEM", ""),
			"ordered_qty": _to_int(r.get("ORQTY")),
			"filled_qty": _to_int(r.get("ORQTYF")),
			"order_price": _to_float(r.get("ORPRC")),
			"filled_price": _to_float(r.get("ORPRCF")),
			"customer_item": r.get("ORCUSI", ""),
			"customer_desc": r.get("ORCUSD", ""),
			"ss_order_number": r.get("ORDNO", ""),
		})
	return rows


def normalize_credits(records: list[dict]) -> list[dict]:
	"""GetCreditsByDate Header+Detail → ``[{credit_no, credit_date, invoice_no,
	po, reason, total, lines: [{sku, qty, price, line_num}]}]``."""
	by_no: dict[str, dict] = {}
	order: list[str] = []
	for r in records:
		no = r.get("CRDNO", "")
		if r["_tag"] == "Header":
			by_no[no] = {
				"credit_no": no, "credit_date": r.get("CRDDATE", ""),
				"invoice_date": r.get("INVDATE", ""), "invoice_no": r.get("INVNO", ""),
				"po": r.get("PONBR", ""), "reason": r.get("REASNM", ""),
				"total": _to_float(r.get("CRDTOT")), "lines": [],
			}
			order.append(no)
		elif r["_tag"] == "Detail":
			by_no.setdefault(no, {"credit_no": no, "po": "", "reason": "", "total": 0.0,
				"invoice_no": "", "credit_date": "", "invoice_date": "", "lines": []})
			if no not in order:
				order.append(no)
			by_no[no]["lines"].append({
				"sku": r.get("ITEMNO", ""), "qty": _to_int(r.get("SQTY")),
				"price": _to_float(r.get("SPRC")), "line_num": _to_int(r.get("LINENUM")),
			})
	return [by_no[n] for n in order]


# ------------------------------------------------------------------ client

class SSOrdersClient:
	"""One instance per job. ``session`` is injectable for tests."""

	def __init__(self, config: SSConfig, session: requests.Session | None = None):
		self.config = config
		self.session = session or requests.Session()
		self._auth_proven = False

	@classmethod
	def from_settings(cls) -> "SSOrdersClient":
		return cls(SSConfig.from_settings())

	# ---- transport ---------------------------------------------------------

	def _auth(self, with_source=True) -> dict:
		c = self.config
		if not (c.customer_number and c.password):
			raise SSConfigError("Sports South Settings: customer number / password missing.")
		# Secret: this dict is a local here and in requests' frames on every
		# failure the Error Log prints (ffl_core.credentials).
		p = {"CustomerNumber": c.customer_number, "UserName": c.username or c.customer_number,
			"Password": Secret(c.password)}
		if with_source:
			p["Source"] = c.source or c.customer_number
		return p

	def _post(self, service: str, method: str, params: dict | None = None, *,
			with_source: bool = True) -> tuple[str, str]:
		"""POST → ``(kind, inner)``. Raises SSError on transport failure, SSAuthError
		on a recognised auth-failure body, SSError(terminal) on an ``<ERROR>``."""
		url = f"{self.config.base_url}/{service}.asmx/{method}"
		data = {**self._auth(with_source), **{k: v for k, v in (params or {}).items()
			if v is not None}}
		try:
			resp = self.session.post(url, data=data, timeout=self.config.timeout,
				headers={"User-Agent": "gunstore-pos-ss/1"})
		except requests.RequestException as exc:
			raise SSError(f"{service}.{method} → network error: {type(exc).__name__}",
				terminal=False) from exc
		if resp.status_code >= 400:
			terminal = 400 <= resp.status_code < 500 and resp.status_code != 429
			raise SSError(f"{service}.{method} → HTTP {resp.status_code}",
				status_code=resp.status_code, terminal=terminal)
		kind, inner = unwrap(resp.text)
		err = sniff_error(inner) if kind in ("string", "raw") else None
		if err:
			if is_auth_error(err):
				raise SSAuthError(f"{service}.{method}: authentication failed", terminal=True)
			raise SSError(f"{service}.{method}: {err}", terminal=True)
		return kind, inner

	def assert_auth(self) -> None:
		"""Prove the credentials with a LOUD method before trusting any silent
		one (ShipCheck / GetEstimatedShipCost answer empty on bad auth)."""
		if self._auth_proven:
			return
		self._post(ORDERS, "GetUnsubmittedOrders")
		self._auth_proven = True

	# ---- orders.asmx: create ----------------------------------------------

	def add_header(self, *, po: str, ship_to: dict, ship_via: str,
			adult_signature: bool, signature: bool, insurance: bool,
			customer_order_number: str = "", sales_message: str = "") -> int:
		"""→ SS web order number. 0 / non-int = failure (raised)."""
		st = ship_to or {}
		zip5 = re.sub(r"\D", "", str(st.get("zip") or ""))[:5]
		kind, inner = self._post(ORDERS, "AddHeader", {
			"PO": po, "CustomerOrderNumber": customer_order_number or "0",
			"SalesMessage": sales_message or "",
			"ShipVIA": ship_via,
			"ShipToName": (st.get("name") or "")[:50],
			"ShipToAttn": (st.get("attn") or "")[:50],
			"ShipToAddr1": (st.get("line1") or "")[:50],
			"ShipToAddr2": (st.get("line2") or "")[:50],
			"ShipToCity": (st.get("city") or "")[:30],
			"ShipToState": (st.get("state") or "")[:2],
			"ShipToZip": zip5 or "0",
			"ShipToPhone": re.sub(r"\D", "", str(st.get("phone") or ""))[:15],
			"AdultSignature": "true" if adult_signature else "false",
			"Signature": "true" if signature else "false",
			"Insurance": "true" if insurance else "false",
		})
		n = _to_int(inner, 0) if kind in ("int", "raw") else 0
		if n <= 0:
			raise SSError(f"AddHeader refused (PO {po}): {inner[:200] or 'returned 0'}",
				terminal=True)
		return n

	def add_detail(self, order_number: int, *, sku: str, qty: int, order_price: float,
			customer_item: str = "", customer_desc: str = "") -> bool:
		kind, inner = self._post(ORDERS, "AddDetail", {
			"OrderNumber": int(order_number), "SSItemNumber": int(sku),
			"Quantity": int(qty), "OrderPrice": f"{float(order_price):.2f}",
			"CustomerItemNumber": (customer_item or "")[:30],
			"CustomerItemDescription": (customer_desc or "")[:60],
		})
		return _bool(inner)

	def submit(self, order_number: int) -> bool:
		_, inner = self._post(ORDERS, "Submit", {"OrderNumber": int(order_number)})
		return _bool(inner)

	def delete_open_order(self, order_number: int) -> bool:
		"""Deletes an UNSUBMITTED order only (WSDL doc). False once submitted."""
		_, inner = self._post(ORDERS, "DeleteOpenOrder", {"OrderNumber": int(order_number)})
		return _bool(inner)

	# ---- orders.asmx: read -------------------------------------------------

	def get_detail(self, order_number: int | str, customer_order_number: str = "") -> list[dict]:
		_, inner = self._post(ORDERS, "GetDetail", {
			"OrderNumber": str(order_number), "CustomerOrderNumber": customer_order_number or ""})
		return normalize_detail_rows(parse_records(inner))

	def get_header(self, order_number: int | str, customer_order_number: str = "") -> list[dict]:
		_, inner = self._post(ORDERS, "GetHeader", {
			"OrderNumber": str(order_number), "CustomerOrderNumber": customer_order_number or ""})
		return parse_records(inner)

	def get_ship_to(self, order_number: int | str) -> dict:
		"""The address an order ships to: the first ``<Table>`` record (WEBNAM,
		WEBAD1, WEBAD2, WEBCTY, WEBSTA, WEBZIP …), or ``{}`` when SS answers an empty
		``<NewDataSet />`` (live-verified 2026-09-29: an order shipped to a consumer
		returns nothing). CustomerOrderNumber must be sent, blank — omitting it is
		HTTP 500."""
		_, inner = self._post(ORDERS, "GetShipTo", {
			"OrderNumber": str(order_number), "CustomerOrderNumber": ""})
		rows = parse_records(inner)
		return rows[0] if rows else {}

	def get_unsubmitted_orders(self) -> list[dict]:
		_, inner = self._post(ORDERS, "GetUnsubmittedOrders")
		return parse_records(inner)

	def ffl_accepts_transfer(self, ffl: str) -> str:
		"""Raw verdict: an expiry date (accepts), ``0`` (does not accept) or
		``UNKNOWN`` (not in SS's database)."""
		self.assert_auth()
		_, inner = self._post(ORDERS, "FFLAcceptsTransfer", {"FFL": ffl})
		return (inner or "").strip()

	def transfer_documents_required(self, ffl: str) -> str:
		"""``Y`` documentation required / ``N`` do not ship / other per WSDL."""
		self.assert_auth()
		_, inner = self._post(ORDERS, "TransferDocumentsRequired", {"FFL": ffl})
		return (inner or "").strip()

	def ship_check(self, zip_code: str, sku: str) -> list[dict]:
		"""Rules blocking this item to this ZIP; [] = compliant. Silent on bad
		auth, hence the loud probe first."""
		self.assert_auth()
		_, inner = self._post(ORDERS, "ShipCheck", {
			"ZipCode": re.sub(r"\D", "", zip_code or "")[:5], "ItemNumber": int(sku)})
		return parse_records(inner)

	# ---- invoices.asmx -----------------------------------------------------

	def get_invoices_by_po(self, po: str) -> list[dict]:
		_, inner = self._post(INVOICES, "GetByPONumber", {"PONumber": po})
		return parse_records(inner)

	def get_tracking_by_po(self, po: str) -> list[dict]:
		_, inner = self._post(INVOICES, "GetTrackingByPo", {"PONumber": po})
		return parse_records(inner)

	def get_invoices_by_date(self, start: str, end: str) -> list[dict]:
		"""Every invoice header on the account dated start..end (``YYYY-MM-DD``,
		live-verified) — S1INV, S1DATE, PONBR (padded), INVGS, ..."""
		_, inner = self._post(INVOICES, "GetByDate", {"StartDate": start, "EndDate": end})
		return parse_records(inner)

	def get_invoice_detail(self, invoice_no: str) -> list[dict]:
		"""Billed lines: SINUM (item #), IDESC, SQTY, SPRC, SGROS, ORDNO. Freight
		rides as its own ``*SHIPPING ...*`` line (live-verified)."""
		_, inner = self._post(INVOICES, "GetDetail", {"InvoiceNumber": invoice_no})
		return parse_records(inner)

	def get_tracking_for_invoice(self, invoice_no: str) -> list[dict]:
		_, inner = self._post(INVOICES, "GetTrackingForInvoice", {"InvoiceNumber": invoice_no})
		return parse_records(inner)

	def get_order_headers(self) -> list[dict]:
		"""Every order header on the account — ORDNO, ORPO (our PO), ORDATE
		(YYMMDD), STATUS — placed from anywhere, not just this POS."""
		_, inner = self._post(ORDERS, "GetHeaders")
		return parse_records(inner)

	def get_serials_for_invoice(self, invoice_no: str) -> list[dict]:
		_, inner = self._post(INVOICES, "GetSerialsForInvoice", {"InvoiceNumber": invoice_no})
		return parse_records(inner)

	def get_credits_by_date(self, yyyymmdd: str) -> list[dict]:
		"""Credit memos issued on that date (call with YESTERDAY: credits post
		overnight). Format pinned here: YYYYMMDD."""
		if not re.fullmatch(r"\d{8}", yyyymmdd or ""):
			raise SSError(f"GetCreditsByDate wants YYYYMMDD, got {yyyymmdd!r}", terminal=True)
		_, inner = self._post(INVOICES, "GetCreditsByDate", {"DateFrom": yyyymmdd})
		return normalize_credits(parse_records(inner))


def _bool(inner: str) -> bool:
	return (inner or "").strip().lower() in ("true", "1")


def probe_connection(client: SSOrdersClient) -> list[dict]:
	"""Hub / Settings Test Connection: one loud read per service. Never raises."""
	checks = []
	for label, fn in (("Orders API (orders.asmx)", client.get_unsubmitted_orders),
			("Invoices API (invoices.asmx)", lambda: client.get_invoices_by_po("POS-PING"))):
		try:
			fn()
			checks.append({"label": label, "ok": True, "detail": "Authenticated."})
		except SSConfigError as exc:
			checks.append({"label": label, "ok": None, "detail": str(exc)})
		except Exception as exc:  # noqa: BLE001 — a probe reports, never aborts
			checks.append({"label": label, "ok": False, "detail": str(exc)})
	return checks
