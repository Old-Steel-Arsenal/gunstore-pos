"""Lipsey's for the local catalog: api.lipseys.com JSON feeds -> the dc_* store.

A port of the Data Service's Lipsey's driver (its catalog and quantity lanes;
its image mirror is not ported: Local mode links Lipsey's own image URL, as it
does for RSR and Sports South). Lanes, in execution order:

  catalog   heavy  Items/CatalogFeed, about every 4 hours (Lipsey's caches it 4
                   hours): the full item list, about 18k rows and 48 MB of JSON,
                   streamed to a temp file after a free-disk check. Refused under
                   MIN_ROWS or with more than 1% of its rows rejected. Stock, cost
                   and allocation are insert-only: the quantity lane owns them. A
                   live row missing from the file is marked discontinued; when more
                   than 2% of the live rows vanish the load stands but nothing is
                   retired until a forced run from bench (Sync now never forces).
  quantity         Items/PricingQuantityFeed, about hourly: stock, cost and
                   allocation for every item. Refused when under 90% of its item
                   numbers match the catalog or more than 10% of the live rows are
                   missing from it; a live row it omits gets qty 0. Waits for the
                   first catalog.

Credentials: the dealer login on Lipseys Settings (email, password, customer
number, API base URL), the same login as ordering, through
``lipseys.api_client`` (our User-Agent, the token in memory only, the body-level
``authorized: false`` check with one re-login).

Gating is on the last ATTEMPT, not the last success, as in the Data Service: a
failing lane backs off (doubling, capped) instead of logging in and pulling
48 MB every tick, and a rejected login is an auth hold: neither lane dials again
until Lipseys Settings is saved. Sync now and a forced run open the schedule
but still wait out a short floor since the last attempt, and never lift the
hold. Every gate fails closed.
"""
from __future__ import annotations

import glob
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import frappe

from ffl_integrations.catalog_local import store, sync
from ffl_integrations.catalog_local.drivers import lipseys_feed, rsr
from ffl_integrations.catalog_local.sync import FeedAuthError, FeedFormatError, FeedUnavailable
from ffl_integrations.lipseys.api_client import (
	DEALER,
	DEFAULT_BASE_URL,
	FEED_TMP_PREFIX,
	SETTINGS_DOCTYPE,
	LipseysAuthError,
	LipseysClient,
	LipseysConfig,
	LipseysError,
	errors_of,
)

CODE = "lipseys"
SETTINGS = SETTINGS_DOCTYPE
CATALOG = "Items/CatalogFeed"
PRICING = "Items/PricingQuantityFeed"

CATALOG_DEADLINE_S, PRICING_DEADLINE_S = 120.0, 60.0  # wall clock per download
CATALOG_MAX_BYTES, PRICING_MAX_BYTES = 256 << 20, 32 << 20  # measured about 48 MB / 3.7 MB

MIN_ROWS = 12_900              # 0.7 x the 18,446 rows measured
DISAPPEARED_FRACTION = 0.02    # the same bar as Sports South's snapshot
QTY_MATCH_FLOOR = 0.90         # measured 18,445 of 18,446 item numbers match
# The quantity lane owns stock, cost AND allocation: a 4-hour-old catalog reload
# never overwrites the hourly values.
INSERT_ONLY_COLS = ("qty", "cost", "status_flag")
# A row the catalog retired (vanished from the file) and that the file carries
# again is re-seeded like a new row, its insert-only columns included.
REVIVE_FLAGS = ("Discontinued",)
DISCONTINUED = "Discontinued"

UNDECRYPTABLE = ("the dealer password in Lipseys Settings cannot be decrypted (the site's "
	"encryption key changed?); enter it again")
HOLD = "the Lipsey's catalog lanes wait until Lipseys Settings is saved again"


@dataclass(frozen=True)
class AttemptGate:
	"""``attempt_due`` over the lane's recent attempts (dc_sync_run rows plus the
	attempt stamp ``sync.mark_attempt`` leaves when a lane starts: a run killed
	without a row still counts). Fails CLOSED: for a vendor that rate-limits and
	allowlists, a database hiccup means "do not dial"."""
	every: timedelta
	max_backoff: timedelta
	force_floor: timedelta

	def due(self, run, lane: str) -> bool:
		try:
			attempts = recent_attempts(run.driver.code, lane)
			saved = credentials_saved_at(run.driver.code)
		except Exception:  # noqa: BLE001
			_log().exception("catalog %s gate read failed; holding", run.source(lane))
			return False
		ok = attempt_due(attempts, run.now, every=self.every, max_backoff=self.max_backoff,
			force=run.tick.gates_open, force_floor=self.force_floor, creds_saved_at=saved)
		if not ok and attempts and attempts[0][2] == "FeedAuthError":
			_log().warning("catalog %s held: Lipsey's rejected the login; save Lipseys Settings "
				"to retry", run.source(lane))
		return ok


CATALOG_GATE = AttemptGate(every=timedelta(hours=3, minutes=50), max_backoff=timedelta(hours=24),
	force_floor=timedelta(minutes=30))
QTY_GATE = AttemptGate(every=timedelta(minutes=50), max_backoff=timedelta(hours=8),
	force_floor=timedelta(minutes=15))


def _log():
	return frappe.logger("catalog_local", allow_site=True)


def attempt_due(attempts, now: datetime, *, every: timedelta, max_backoff: timedelta,
		force: bool, force_floor: timedelta, creds_saved_at) -> bool:
	"""The Data Service's gate over a lane's recent attempts ``[(status, at,
	error_class)]``, newest first.

	- A FeedAuthError holds the lane until the credentials are saved after it
	  (``creds_saved_at``), a forced run included; that save is the operator's retry,
	  so the lane is due at once.
	- ``force`` (Sync now, a forced run) runs once ``force_floor`` has passed since
	  the last attempt, so a button cannot hammer the vendor.
	- Otherwise: ``every`` after a success; after N failures in a row,
	  ``every x 2^(N-1)``, capped at ``max_backoff``."""
	if not attempts:
		return True
	status, last_at, err = attempts[0]
	if status == "failed" and err == "FeedAuthError":
		return creds_saved_at is not None and creds_saved_at > last_at
	age = now - last_at
	if force:
		return age >= force_floor
	if status == "success":
		return age >= every
	streak = next((i for i, a in enumerate(attempts) if a[0] != "failed"), len(attempts))
	return age >= min(every * 2 ** (streak - 1), max_backoff)


def recent_attempts(code: str, lane: str, limit: int = 8) -> list[tuple]:
	"""The lane's last attempts, newest first: ``[(status, finished_at, error_class)]``.
	An attempt stamp newer than the newest row is a run that died without recording
	one (a job timeout, a restart): it counts as a failed attempt at its start."""
	rows = [tuple(r) for r in frappe.db.sql("""SELECT status, finished_at, error_class
		FROM dc_sync_run WHERE source = %s ORDER BY finished_at DESC, id DESC LIMIT %s""",
		(f"{code}_{lane}", int(limit)))]
	marker = sync.lane_attempts().get(code, {}).get(lane)
	if marker is not None and (not rows or marker > rows[0][1]):
		rows.insert(0, ("failed", marker, "Interrupted"))
	return rows[:limit]


def credentials_saved_at(code: str = CODE):
	rows = frappe.db.sql("SELECT creds_saved_at FROM dc_meta WHERE distributor = %s", (code,))
	return rows[0][0] if rows else None


def note_credentials_saved(code: str = CODE) -> None:
	"""Lipseys Settings was saved: lift an auth hold (``attempt_due``). Called from the
	form's on_update only; the shipping poll's own writes to the document do not
	call it."""
	frappe.db.sql("""INSERT INTO dc_meta (distributor, creds_saved_at) VALUES (%s, %s)
		ON DUPLICATE KEY UPDATE creds_saved_at = VALUES(creds_saved_at)""", (code, store.utc_now()))


def dealer_email() -> str:
	return (frappe.db.get_single_value(SETTINGS, "dealer_email") or "").strip()


def credentials() -> LipseysConfig | None:
	"""The dealer login on Lipseys Settings, or None when its email or password is
	blank. Read fresh: a re-saved password takes effect on the next lane. A
	password that is saved but cannot be decrypted raises FeedAuthError."""
	s = frappe.get_single(SETTINGS)
	email = (s.get("dealer_email") or "").strip()
	if not email:
		return None
	password = rsr.saved_password(s, SETTINGS, "dealer_password", UNDECRYPTABLE)
	if not password:
		return None
	return LipseysConfig(email=email, password=password, account=DEALER,
		customer_number=(s.get("customer_number") or "").strip(),
		base_url=((s.get("base_url") or "").strip() or DEFAULT_BASE_URL).rstrip("/"))


def _mask(text: str) -> str:
	"""Server text lands in dc_sync_run.error_msg: no email address in it."""
	return re.sub(r"\S+@\S+", "<email>", str(text))


def sweep_temp(max_age_s: float = 3600.0) -> None:
	"""A job killed mid-download never reaches its ``finally``: drop our feed temp
	files older than an hour (up to CATALOG_MAX_BYTES each)."""
	cutoff = time.time() - max_age_s
	for path in glob.glob(os.path.join(tempfile.gettempdir(), f"{FEED_TMP_PREFIX}*")):
		try:
			if os.path.getmtime(path) < cutoff:
				os.unlink(path)
		except OSError:
			pass


def fetch(endpoint: str, *, expect: type, max_bytes: int, deadline_s: float,
		client: LipseysClient | None = None):
	"""One feed's ``data``: log in, GET the feed into a temp file, parse it. A refused
	login (or one that answers for another account, or HTTP 401 / 403 at login, an
	address off Lipsey's allowlist) is FeedAuthError; transport failures and a token
	refused after a fresh login are FeedUnavailable; an unsuccessful or empty
	envelope is FeedFormatError."""
	if client is None:
		config = credentials()
		if config is None:
			raise FeedAuthError(f"Lipseys Settings has no dealer email and password; {HOLD}")
		if not config.base_url.startswith("https://"):
			raise FeedAuthError(f"the API base URL in Lipseys Settings is not https; {HOLD}")
		client = LipseysClient(config)
	try:
		try:
			client.login()
		except LipseysError as exc:
			if isinstance(exc, LipseysAuthError) or exc.status_code in (401, 403):
				raise FeedAuthError(f"{_mask(exc)}; {HOLD}") from None
			raise FeedUnavailable(_mask(exc)) from None
		try:
			env = client.get_feed(endpoint, max_bytes=max_bytes, deadline_s=deadline_s)
		except LipseysAuthError as exc:
			raise FeedAuthError(f"{_mask(exc)}; {HOLD}") from None
		except LipseysError as exc:
			raise FeedUnavailable(_mask(exc)) from None
	finally:
		close = getattr(client.session, "close", None)
		if close:
			close()
	if env.get("success") is not True:
		raise FeedFormatError(f"Lipsey's {endpoint}: success=false "
			f"({_mask(errors_of(env))[:160] or 'no detail'})")
	data = env.get("data")
	if not isinstance(data, expect) or not data:
		# A full feed is never empty: an empty one is a server fault.
		raise FeedFormatError(f"Lipsey's {endpoint}: data is empty or not a {expect.__name__}")
	return data


class LipseysDriver(sync.Driver):
	label = "Lipsey's"
	feed_stale = {"quantity": 10_800, "catalog": 43_200}

	def __init__(self, code: str = CODE, client_factory=None):
		self.code = code
		# None: a client over Lipseys Settings' dealer login (tests pass a fake)
		self.client_factory = client_factory
		self._lanes = (
			sync.Lane("catalog", gate=lambda run: CATALOG_GATE.due(run, "catalog"),
				run=self._catalog, heavy=True, timeout=sync.HEAVY_TIMEOUT_SECONDS),
			sync.Lane("quantity", gate=self._quantity_due, run=self._quantity),
		)

	def lanes(self):
		return self._lanes

	def configured(self) -> bool:
		"""An enabled Distributor buys through Lipsey's and the dealer email is saved.
		The password is not read here: one that is missing or cannot be decrypted
		fails the lanes out loud (a failed run and an auth hold) instead of skipping them."""
		return super().configured() and bool(dealer_email())

	def _fetch(self, endpoint: str, **kw):
		sweep_temp()
		client = self.client_factory() if self.client_factory else None
		return fetch(endpoint, client=client, **kw)

	def _live_skus(self) -> set:
		return set(frappe.db.sql_list(f"""SELECT sku FROM dc_offer WHERE distributor = %s
			AND NOT discontinued AND NOT {store.deleted_sql()}""", (self.code,)))

	def _catalog(self, run) -> dict:
		# The same free-disk guard as the RSR and Sports South downloads, before any request.
		rsr.check_disk(tempfile.gettempdir(), CATALOG_MAX_BYTES, CATALOG)
		# lazy: the parsed feed is held in memory (48 MB of JSON, a few hundred MB as Python objects); stream it with an incremental JSON decoder if the feed outgrows the catalog worker
		data = self._fetch(CATALOG, expect=list, max_bytes=CATALOG_MAX_BYTES,
			deadline_s=CATALOG_DEADLINE_S)
		present = lipseys_feed.catalog_skus(data)
		live = self._live_skus()
		stats: dict = {}
		out = store.load_catalog(lipseys_feed.iter_catalog(data, stats), distributor=self.code,
			mode="snapshot", insert_only_cols=INSERT_ONLY_COLS, revive_flags=REVIVE_FLAGS,
			min_rows=MIN_ROWS)
		if out.get("lock_skipped"):
			sync.clear_attempt(self.code, "catalog")  # a busy lock is not a vendor failure
			return out
		out["rejected"] = stats.get("rejected", 0)
		missing = sorted(live - present)
		if missing:
			# The load above only upserts what the file carries (MIN_ROWS and the
			# half-size rule refuse a short file); retiring rows is the step a wrong
			# file could damage, so over the bar it waits for a forced run.
			if len(missing) > len(live) * DISAPPEARED_FRACTION and not run.tick.force:
				raise FeedFormatError(
					f"Lipsey's catalog loaded, but {len(missing)} of {len(live)} live rows vanished "
					f"(more than {DISAPPEARED_FRACTION:.0%}) - not retiring them; a forced run applies "
					f"the diff: sync.run_lane({self.code!r}, 'catalog', force=True)")
			d = store.apply_deleted(missing, distributor=self.code, status_flag=DISCONTINUED)
			if d.get("lock_skipped"):
				raise RuntimeError("write lock busy while retiring vanished rows - the next catalog retries")
			out["disappeared"] = d["flagged"]
		return out

	def _quantity_due(self, run) -> bool:
		# Waits (no row) until the first catalog has landed: the pricing rows only
		# update offers the catalog created.
		try:
			has_offers = bool(frappe.db.sql("SELECT 1 FROM dc_offer WHERE distributor = %s LIMIT 1",
				(self.code,)))
		except Exception:  # noqa: BLE001 — fail closed
			_log().exception("catalog %s gate read failed; holding", run.source("quantity"))
			return False
		return has_offers and QTY_GATE.due(run, "quantity")

	def _quantity(self, run) -> dict:
		data = self._fetch(PRICING, expect=dict, max_bytes=PRICING_MAX_BYTES,
			deadline_s=PRICING_DEADLINE_S)
		stats: dict = {}
		rows = lipseys_feed.pricing_rows(data, stats)
		listed = {r[0] for r in rows}
		known = set(frappe.db.sql_list("SELECT sku FROM dc_offer WHERE distributor = %s",
			(self.code,)))
		matched = len(listed & known)
		if matched < len(rows) * QTY_MATCH_FLOOR:
			raise FeedFormatError(f"Lipsey's pricing: only {matched} of {len(rows)} item numbers "
				"match the catalog - refusing the file")
		# The feed prices every item Lipsey's carries: a live row it omits (one that
		# vanished from a catalog whose retiring waits for a forced run) has no stock.
		live = self._live_skus()
		absent = live - listed
		if len(absent) > len(live) * (1 - QTY_MATCH_FLOOR):
			raise FeedFormatError(f"Lipsey's pricing: {len(absent)} of {len(live)} live items "
				"missing - refusing the file")
		out = store.apply_quantity(rows + [(s, 0, None, None) for s in sorted(absent)],
			distributor=self.code)
		if out.get("lock_skipped"):
			sync.clear_attempt(self.code, "quantity")
			return out
		out.update(absent=len(absent), rejected=stats["rejected"], next_update=stats["next_update"])
		return out


DRIVER = sync.register(LipseysDriver())
