"""Lipsey's ordering client — api.lipseys.com/api/Integration, JSON over HTTPS.

Wire facts (Lipsey's API docs + a read-only live probe):

* **Login per session.** ``POST Authentication/Login {Email, Password}`` answers
  ``{token, econtact}``; the token rides in a ``Token`` header on every later
  call. Its lifetime is undocumented, so a client logs in on first use and keeps
  the token in memory only — never in the database, never in a log.
* **An expired or missing token is HTTP 200** with ``{"success": false,
  "authorized": false}``. The body decides, never the status alone. Such a call
  did nothing at Lipsey's, so it is retried once after a fresh login.
* **Envelope** ``{success, authorized, errors[], data}`` on every call but login.
* **Cloudflare** sits in front and refuses the default ``Python-urllib`` user
  agent: every request names its own.
* **Two logins.** The dealer login places store orders (they ship to the address
  on that Lipsey's account); drop-ship orders need Lipsey's separate drop-ship
  login. A client is bound to one of them.
* The API is IP-allowlisted, so nothing in development or CI can reach it: every
  test drives this client through a fake session.

Placing an order is money. A placement whose answer never arrived (a timeout or
a reset after the request left, a 5xx, an unreadable body) may or may not have
landed, and Lipsey's documents no way to ask by PO number — so it is NEVER
retried here; it raises ``LipseysPlacementUnknown`` for a human to check.

The catalog feeds (``get_feed``, used by the local catalog) are GETs whose
answer is streamed to a temp file under a byte cap and a wall-clock deadline,
then parsed.

``frappe`` is imported only inside ``LipseysConfig.from_settings``.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import requests

from ffl_core.credentials import RedactedRepr, Secret

log = logging.getLogger(__name__)

SETTINGS_DOCTYPE = "Lipseys Settings"
DEFAULT_BASE_URL = "https://api.lipseys.com/api/Integration"
DEFAULT_TIMEOUT = 30
USER_AGENT = "opengunstore-lipseys/1"
FEED_TMP_PREFIX = "lipseys-feed-"

EP_LOGIN = "Authentication/Login"
EP_VALIDATE_ITEM = "Items/ValidateItem"
EP_API_ORDER = "Order/APIOrder"
EP_DROPSHIP = "Order/DropShip"
EP_DROPSHIP_FIREARM = "Order/DropShipFirearm"
EP_ONE_DAY = "Shipping/OneDay"

# The two logins, keyed like the lanes' accounts (distributor.lanes).
DEALER = "dealer"
FULFILLMENT = "fulfillment"
_LOGIN_FIELDS = {DEALER: ("dealer_email", "dealer_password"),
	FULFILLMENT: ("dropship_email", "dropship_password")}
_LOGIN_LABEL = {DEALER: "dealer", FULFILLMENT: "drop-ship"}


class LipseysError(RuntimeError):
	"""A Lipsey's call failed. ``terminal`` = needs a human (a refusal, a bad
	item); ``maybe_sent`` = the request may have been acted on at Lipsey's (no
	answer, a 5xx, an unreadable answer) — only a placement cares."""

	def __init__(self, *args, status_code: int | None = None, terminal: bool = False,
			maybe_sent: bool = False):
		super().__init__(*args)
		self.status_code = status_code
		self.terminal = terminal
		self.maybe_sent = maybe_sent


class LipseysConfigError(LipseysError):
	"""Settings incomplete or ordering switched off. Fixed in Lipseys Settings."""


class LipseysAuthError(LipseysError):
	"""The login was refused, or answered for another Lipsey's account."""


class LipseysPlacementUnknown(LipseysError):
	"""An order was sent and no usable answer came back: it may or may not exist
	at Lipsey's. Never retried automatically."""


@dataclass(repr=False)
class LipseysConfig(RedactedRepr):
	email: str
	password: str
	account: str = DEALER
	customer_number: str = ""
	base_url: str = DEFAULT_BASE_URL
	timeout: int = DEFAULT_TIMEOUT

	@classmethod
	def from_settings(cls, account: str = DEALER) -> "LipseysConfig":
		import frappe

		s = frappe.get_single(SETTINGS_DOCTYPE)
		email_field, password_field = _LOGIN_FIELDS[account]
		return cls(
			email=(s.get(email_field) or "").strip(),
			password=s.get_password(password_field, raise_exception=False) or "",
			account=account,
			# The customer number names the dealer account; the drop-ship login is a
			# separate Lipsey's account with its own number, so it is not checked.
			customer_number=(s.customer_number or "").strip() if account == DEALER else "",
			base_url=(s.base_url or DEFAULT_BASE_URL).rstrip("/"),
		)


def login_configured(settings, account: str) -> bool:
	"""Both halves of that login are filled in (a Password field reads as its
	mask on the document, which is enough to tell it is set)."""
	email_field, password_field = _LOGIN_FIELDS[account]
	return bool((settings.get(email_field) or "").strip() and settings.get(password_field))


def one_day_param(day: date) -> str:
	"""OneDay's date, spelled as its docs spell it: ``7/20/2019``."""
	return f"{day.month}/{day.day}/{day.year}"


def errors_of(envelope: dict) -> str:
	errors = (envelope or {}).get("errors") or []
	if isinstance(errors, str):
		errors = [errors]
	return "; ".join(str(e).strip() for e in errors if str(e).strip())


class LipseysClient:
	"""One login, one in-memory token. ``session`` is injectable for tests."""

	def __init__(self, config: LipseysConfig, session: requests.Session | None = None):
		self.config = config
		self.session = session or requests.Session()
		self._token: Secret | None = None
		self.contact: dict = {}

	@classmethod
	def from_settings(cls, account: str = DEALER) -> "LipseysClient":
		return cls(LipseysConfig.from_settings(account))

	@property
	def label(self) -> str:
		return _LOGIN_LABEL.get(self.config.account, self.config.account)

	# ---- transport ---------------------------------------------------------

	def _send(self, path: str, body, *, with_token: bool = True) -> dict:
		"""POST → the parsed JSON object. Transport and HTTP failures raise
		``LipseysError`` with ``maybe_sent`` telling whether the request may have
		reached Lipsey's: only a connect timeout is certain it did not; a 4xx was
		answered and refused."""
		url = f"{self.config.base_url.rstrip('/')}/{path}"
		headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
		if with_token and self._token:
			headers["Token"] = self._token
		try:
			resp = self.session.post(url, json=body, headers=headers, timeout=self.config.timeout)
		except requests.RequestException as exc:
			raise LipseysError(f"Lipsey's {path}: network error ({type(exc).__name__})",
				maybe_sent=not isinstance(exc, requests.ConnectTimeout)) from exc
		if resp.status_code >= 400:
			raise LipseysError(f"Lipsey's {path}: HTTP {resp.status_code}",
				status_code=resp.status_code,
				terminal=400 <= resp.status_code < 500 and resp.status_code != 429,
				maybe_sent=resp.status_code >= 500)
		try:
			data = resp.json()
		except ValueError as exc:
			raise LipseysError(f"Lipsey's {path}: the answer is not JSON",
				status_code=resp.status_code, maybe_sent=True) from exc
		if not isinstance(data, dict):
			raise LipseysError(f"Lipsey's {path}: unexpected answer shape",
				status_code=resp.status_code, maybe_sent=True)
		return data

	def login(self) -> dict:
		"""Sign in; keep the token. Returns the econtact ``data`` (name, cusNo,
		locationName…). A configured customer number (dealer login only) must match
		the account the login answers for — a login for the wrong account must not
		place orders."""
		c = self.config
		if not (c.email and c.password):
			raise LipseysConfigError(
				f"Lipseys Settings: the {self.label} login (email and password) is not filled in.")
		data = self._send(EP_LOGIN, {"Email": c.email, "Password": Secret(c.password)},
			with_token=False)
		contact = data.get("econtact") or {}
		token = str(data.get("token") or "").strip()
		if not token or contact.get("authorized") is False or contact.get("success") is False:
			reason = errors_of(contact) or "no token returned"
			raise LipseysAuthError(f"Lipsey's refused the {self.label} login: {reason}",
				terminal=True)
		info = contact.get("data") or {}
		expected = (c.customer_number or "").strip()
		got = str(info.get("cusNo") or "").strip()
		if expected and got and got != expected:
			raise LipseysAuthError(
				f"The {self.label} login answers for Lipsey's customer {got}, but Lipseys "
				f"Settings names customer {expected}.", terminal=True)
		self._token = Secret(token)
		self.contact = info
		return info

	def _call(self, path: str, body) -> dict:
		"""Authenticated call → the envelope. ``authorized: false`` (expired token)
		means Lipsey's did nothing: log in again and send once more."""
		if not self._token:
			self.login()
		env = self._send(path, body)
		if env.get("authorized") is False:
			self._token = None
			self.login()
			env = self._send(path, body)
			if env.get("authorized") is False:
				raise LipseysAuthError(f"Lipsey's {path}: not authorized after a fresh login "
					f"({errors_of(env) or 'Not Authorized'}).", terminal=True)
		return env

	def get_feed(self, path: str, *, max_bytes: int, deadline_s: float) -> dict:
		"""GET one catalog feed (Items/CatalogFeed, Items/PricingQuantityFeed) -> the
		parsed envelope. ``authorized: false`` drops the token, logs in again and
		reads once more; a second refusal is a plain ``LipseysError`` (the password
		was accepted, so it is not an auth failure). A refused re-login raises
		``LipseysAuthError`` as ``login`` does."""
		if not self._token:
			self.login()
		env = self._get_json(path, max_bytes=max_bytes, deadline_s=deadline_s)
		if env.get("authorized") is False:
			self._token = None
			self.login()
			env = self._get_json(path, max_bytes=max_bytes, deadline_s=deadline_s)
			if env.get("authorized") is False:
				raise LipseysError(f"Lipsey's {path}: token refused after a fresh login.")
		return env

	def _get_json(self, path: str, *, max_bytes: int, deadline_s: float) -> dict:
		"""Stream a GET to a temp file under a WALL-clock deadline and a byte cap (the
		session timeout is per read: a trickling answer would otherwise run into the
		job timeout), then parse it; the file is always deleted. A non-JSON answer (a
		Cloudflare page) or an unreadable body raises ``LipseysError``; messages
		name the endpoint only."""
		url = f"{self.config.base_url.rstrip('/')}/{path}"
		headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
		if self._token:
			headers["Token"] = self._token
		deadline = time.monotonic() + deadline_s
		fd, tmp = tempfile.mkstemp(prefix=FEED_TMP_PREFIX, suffix=".json")
		try:
			with os.fdopen(fd, "wb") as out:
				try:
					with self.session.get(url, headers=headers, timeout=self.config.timeout,
							stream=True) as resp:
						if resp.status_code != 200:
							raise LipseysError(f"Lipsey's {path}: HTTP {resp.status_code}",
								status_code=resp.status_code)
						if "json" not in (resp.headers.get("Content-Type") or "").lower():
							raise LipseysError(f"Lipsey's {path}: the answer is not JSON",
								status_code=resp.status_code)
						size = 0
						for chunk in resp.iter_content(1 << 16):
							size += len(chunk)
							if size > max_bytes:
								raise LipseysError(f"Lipsey's {path}: answer over {max_bytes:,} bytes")
							if time.monotonic() > deadline:
								raise LipseysError(f"Lipsey's {path}: download exceeded {deadline_s:.0f} s")
							out.write(chunk)
				except requests.RequestException as exc:
					# The class name only: a library message can carry request details.
					raise LipseysError(f"Lipsey's {path}: network error ({type(exc).__name__})") from None
			try:
				with open(tmp, "rb") as f:
					body = json.load(f, parse_float=Decimal)
			except ValueError:
				raise LipseysError(f"Lipsey's {path}: the answer is not valid JSON") from None
		finally:
			try:
				os.unlink(tmp)
			except OSError:
				pass
		if not isinstance(body, dict):
			raise LipseysError(f"Lipsey's {path}: unexpected answer shape")
		return body

	# ---- reads -------------------------------------------------------------

	def validate_item(self, item_no: str) -> dict | None:
		"""Live qty / price / blocked / allocated / canDropship for one item, or
		None when Lipsey's does not know it (``success: false``)."""
		env = self._call(EP_VALIDATE_ITEM, str(item_no))
		data = env.get("data")
		return data if env.get("success") and isinstance(data, dict) else None

	def one_day(self, day: date) -> list[dict]:
		"""Shipping rows (invoiceNumber, orderNumber, poNumber, trackingNumber…)
		for the invoices posted on ``day``, for the whole account."""
		env = self._call(EP_ONE_DAY, one_day_param(day))
		if not env.get("success"):
			raise LipseysError(f"Lipsey's {EP_ONE_DAY} {one_day_param(day)}: "
				f"{errors_of(env) or 'unsuccessful'}", terminal=True)
		data = env.get("data")
		return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

	# ---- orders ------------------------------------------------------------

	def place(self, endpoint: str, body: dict) -> dict:
		"""Send ONE order and return Lipsey's envelope as it answered (the provider
		reads success / errors / per-line results). Never retried: an answer that
		did not come back raises ``LipseysPlacementUnknown``."""
		try:
			return self._call(endpoint, body)
		except LipseysPlacementUnknown:
			raise
		except LipseysError as exc:
			if not exc.maybe_sent:
				raise
			raise LipseysPlacementUnknown(str(exc), status_code=exc.status_code,
				terminal=True, maybe_sent=True) from exc


def probe(client: LipseysClient) -> dict:
	"""Test Connection: log in and make one small read (a ValidateItem of an item
	number nobody carries — read-only). One check row; never raises."""
	label = f"Ordering API ({client.label} login)"
	try:
		info = client.login()
		client.validate_item("POS-PING")
	except LipseysConfigError as exc:
		return {"label": label, "ok": None, "detail": str(exc)}
	except Exception as exc:  # noqa: BLE001 — a probe reports, never aborts
		return {"label": label, "ok": False, "detail": str(exc)}
	who = " · ".join(str(info.get(k) or "").strip() for k in ("name", "locationName")
		if str(info.get(k) or "").strip())
	customer = str(info.get("cusNo") or "").strip()
	detail = "Signed in" + (f" as {who}" if who else "") + (f" (customer {customer})" if customer else "") + "."
	return {"label": label, "ok": True, "detail": detail}
