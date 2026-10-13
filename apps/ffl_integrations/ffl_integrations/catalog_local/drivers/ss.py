"""Sports South for the local catalog: inventory.asmx over HTTP POST -> the dc_* store.

A port of the Data Service's Sports South driver. Lanes, in execution order (the
tick enqueues them in this order on the one catalog worker):

  quantity              every tick. IncrementalOnhandUpdate from the cursor, which
                        is the server's own time echoed back verbatim (an
                        inventory.asmx time is a Central wall clock with a fake
                        +00:00). When the incremental buffer is idle, the full
                        OnhandUpdate, at most every 30 minutes; the lane's job
                        timeout covers that full refresh.
  snapshot       heavy  weekly full DailyItemUpdate rebuild. Sports South has no
                        deletion feed, so a row missing from the snapshot is
                        discontinued; refused when more than 2% of the live rows
                        vanish. Runs when due or forced from bench, never on Sync now.
  catalog               daily DailyItemUpdate delta on its own daily cadence, with a
                        one-day overlap from the newer of the last delta and the last
                        snapshot, once a snapshot has loaded.
  text           heavy  daily ListNewText -> long descriptions.

Credentials: Sports South Settings (account number, API user name, API password,
API base URL), the same login as ordering. Responses stream to a temp file (the
large ones only after a free-disk check) and are deleted after the lane. Transport
failures are FeedUnavailable / FeedAuthError, layout failures FeedFormatError; no
message carries a credential.

Category 31 holds no guns. That is the Data Service's owner rule: its class rule
files a light as an Accessory, and the local backend's legal answer reads the rule
from the driver's ``never_firearm_departments``. It never changes a row's FFL flag.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import frappe
import requests

from ffl_core.credentials import Secret
from ffl_integrations.catalog_local import store, sync
from ffl_integrations.catalog_local.drivers import rsr, ss_feed
from ffl_integrations.catalog_local.sync import FeedUnavailable
from ffl_integrations.sports_south.api_client import DEFAULT_BASE_URL

CODE = "ss"
SETTINGS = "Sports South Settings"
USER_AGENT = "gunstore-pos-catalog/1"
TIMEOUT_SMALL = 60
TIMEOUT_ONHAND_FULL = 900  # the Data Service's budget for the full on-hand list
TIMEOUT_SNAPSHOT = 900

SNAPSHOT_STALE = timedelta(days=6, hours=12)
CATALOG_STALE = timedelta(hours=20)
TEXT_STALE = timedelta(hours=20)
ONHAND_FULL_STALE = timedelta(minutes=30)
REWIND = timedelta(hours=24)
SNAPSHOT_MIN_ROWS = 40_000  # a full Sports South snapshot runs near 59k rows
DISAPPEARED_FRACTION = 0.02
EXCLUDE_COLS = ("long_description",)  # the text lane owns it
INSERT_ONLY_COLS = ("qty", "cost")  # the quantity lane owns them
LARGE_METHODS = frozenset({"DailyItemUpdate", "OnhandUpdate", "ListNewText"})
LARGE_DOWNLOAD_BYTES = 640 << 20  # a full item list and its unwrapped copy

_CENTRAL = ZoneInfo("America/Chicago")

UNDECRYPTABLE = ("the API password in Sports South Settings cannot be decrypted (the site's "
	"encryption key changed?); enter it again")


def _fmt_onhand_since(dt_central: datetime) -> str:
	return dt_central.strftime("%Y-%m-%dT%H:%M:%S")


def _fmt_date(d: date) -> str:
	return f"{d.month}/{d.day}/{d.year}"  # no zero padding: the service's own format


@dataclass(frozen=True)
class FeedCredentials:
	customer_number: str
	username: str
	# Out of the repr, and underscored: the Error Log's traceback formatter
	# prints every public attribute.
	_password: str = field(repr=False)
	base_url: str = DEFAULT_BASE_URL

	@property
	def password(self) -> str:
		return self._password

	def url(self, method: str) -> str:
		base = self.base_url.rstrip("/")
		return f"{base}/inventory.asmx/{method}"


def credentials() -> FeedCredentials | None:
	"""Sports South Settings' login, or None when the account number or the password
	is blank. Read fresh: a re-saved password takes effect on the next lane. A
	password that is saved but cannot be decrypted raises FeedAuthError."""
	s = frappe.get_single(SETTINGS)
	customer = (s.get("customer_number") or "").strip()
	if not customer:
		return None
	password = rsr.saved_password(s, SETTINGS, "password", UNDECRYPTABLE)
	if not password:
		return None
	username = (s.get("username") or "").strip() or customer
	base_url = (s.get("base_url") or "").strip() or DEFAULT_BASE_URL
	return FeedCredentials(customer_number=customer, username=username, _password=password,
		base_url=base_url)


# ------------------------------------------------------------------ quantity cursor

def qty_cursor(code: str) -> str:
	rows = frappe.db.sql("SELECT qty_cursor FROM dc_meta WHERE distributor = %s", (code,))
	return (rows[0][0] or "") if rows else ""


def set_qty_cursor(code: str, value: str, *, expect: str | None = None,
		only_if_earlier: bool = False) -> None:
	"""Move the Sports South quantity cursor. The cursor sits outside the house write
	lock, as in the Data Service. ``only_if_earlier`` (a rewind) moves it back only;
	otherwise it is a compare-and-set against ``expect``, so a rewind that landed
	meanwhile wins."""
	frappe.db.sql("INSERT IGNORE INTO dc_meta (distributor) VALUES (%s)", (code,))
	if only_if_earlier:
		frappe.db.sql("""UPDATE dc_meta SET qty_cursor = %s WHERE distributor = %s
			AND (qty_cursor = '' OR qty_cursor > %s)""", (value, code, value))
	else:
		frappe.db.sql("""UPDATE dc_meta SET qty_cursor = %s WHERE distributor = %s
			AND qty_cursor = %s""", (value, code, expect or ""))
	frappe.db.commit()


# ------------------------------------------------------------------ transport

class InventoryTransport:
	"""Sports South's inventory.asmx: one HTTP POST per method, the response streamed
	to a temp file (the caller deletes it)."""

	def __init__(self, creds: FeedCredentials | None = None, session=None):
		self._creds = creds
		self._session = session

	def credentials(self) -> FeedCredentials:
		creds = self._creds or credentials()
		if creds is None:
			raise FeedUnavailable("Sports South Settings is missing the account number or the API password")
		return creds

	def download(self, method: str, params: dict | None = None, *,
			timeout: int = TIMEOUT_SMALL) -> str:
		creds = self.credentials()
		if method in LARGE_METHODS:
			# The same free-disk guard as the RSR transport: a full disk would take the
			# database down with it. It runs before any temp file or request.
			rsr.check_disk(tempfile.gettempdir(), LARGE_DOWNLOAD_BYTES, method)
		# POST, never GET: the credentials travel in the form body, never in a URL or a log line.
		data = {"UserName": creds.username, "Password": Secret(creds.password),
			"CustomerNumber": creds.customer_number, "Source": creds.customer_number,
			**(params or {})}
		fd, path = tempfile.mkstemp(prefix=f"ss-feed-{method}-", suffix=".xml")
		try:
			with os.fdopen(fd, "wb") as out:
				session = self._session or requests
				with session.post(creds.url(method), data=data, timeout=timeout, stream=True,
						headers={"User-Agent": USER_AGENT}) as resp:
					if resp.status_code != 200:
						raise FeedUnavailable(f"Sports South {method} returned HTTP {resp.status_code}")
					for chunk in resp.iter_content(1 << 20):
						out.write(chunk)
		except requests.RequestException as exc:
			rsr._unlink(path)
			# The class name only: a library message can carry request details.
			raise FeedUnavailable(f"Sports South {method} failed: {type(exc).__name__}") from None
		except BaseException:
			rsr._unlink(path)
			raise
		return path

	def probe(self) -> dict:
		path = self.download("CategoryUpdate", timeout=30)
		try:
			names, _slots = ss_feed.parse_categories(path)
			return {"categories": len(names)}
		finally:
			ss_feed.unlink_quiet(path)


# ------------------------------------------------------------------ driver

class SsDriver(sync.Driver):
	label = "Sports South"
	feed_stale = {"quantity": 5400, "snapshot": 777600, "catalog": 129600, "text": 129600}
	# The Data Service's owner rule. The local backend's legal answer reads it; the
	# product class already files these rows as Accessory, and the flag is never changed.
	never_firearm_departments = ss_feed.NEVER_FIREARM_CATEGORIES

	def __init__(self, code: str = CODE, transport=None):
		self.code = code
		self.transport = transport  # None: InventoryTransport over Sports South Settings (tests pass a fake)
		self._lanes = (
			# An idle buffer makes this lane download and apply the full on-hand list, up to
			# TIMEOUT_ONHAND_FULL for the download alone; the default quantity timeout would kill it every time.
			sync.Lane("quantity", gate=lambda run: True, run=self._quantity,
				timeout=sync.HEAVY_TIMEOUT_SECONDS),
			sync.Lane("snapshot", gate=self._snapshot_due, run=self._snapshot, heavy=True,
				timeout=sync.HEAVY_TIMEOUT_SECONDS),
			# A delta after a few missed days is large: it runs as a heavy lane (one at a time,
			# never ahead of the quantity lanes) with the long job timeout.
			sync.Lane("catalog", gate=self._catalog_due, run=self._catalog, heavy=True,
				timeout=sync.HEAVY_TIMEOUT_SECONDS),
			sync.Lane("text", gate=self._text_due, run=self._text, heavy=True,
				timeout=sync.HEAVY_TIMEOUT_SECONDS),
		)

	def lanes(self):
		return self._lanes

	def configured(self) -> bool:
		"""An enabled Distributor buys through Sports South and an account number is
		saved. The password is not read here: one that is missing or cannot be decrypted
		fails the lanes out loud (a failed run) instead of skipping them."""
		return super().configured() and bool(
			(frappe.db.get_single_value(SETTINGS, "customer_number") or "").strip())

	def _fetch(self, method: str, params, handler, *, timeout: int = TIMEOUT_SMALL):
		path = (self.transport or InventoryTransport()).download(method, params, timeout=timeout)
		try:
			return handler(path)
		finally:
			rsr._unlink(path)

	def _content_base(self, run):
		"""The newest content load (a delta or a snapshot), or None before the first snapshot."""
		bases = [t for t in (sync.last_success(run.source("catalog")),
			sync.last_success(run.source("snapshot"))) if t is not None]
		return max(bases, default=None)

	def _snapshot_due(self, run) -> bool:
		# Sync now does not rebuild the snapshot: the cheap delta brings content current, as in
		# the Data Service. A forced run from bench rebuilds it now.
		return run.tick.force or sync.is_due(run.source("snapshot"), SNAPSHOT_STALE, run.now)

	def _catalog_due(self, run) -> bool:
		# The Data Service's delta gate, in this order.
		if run.state.get("snapshot_loaded"):
			return False  # the snapshot that just landed covers today
		# The delta keeps its OWN daily cadence: its own last success, not the snapshot's.
		if not run.due("catalog", CATALOG_STALE):
			return False  # Sync now opens this gate
		base = self._content_base(run)
		if base is None:
			return False  # the delta waits for a first snapshot
		run.state["delta_base"] = base
		return True

	def _text_due(self, run) -> bool:
		if self._content_base(run) is None:
			return False  # no offers to hang text on; keeps the 1990 backfill for the first real run
		if not run.due("text", TEXT_STALE):
			return False
		run.state["text_base"] = sync.last_success(run.source("text"))
		return True

	def _load_items(self, run, *, mode: str, last_update: str, min_rows: int, present,
			timeout: int) -> dict:
		started = datetime.now(_CENTRAL)
		names, slots = self._fetch("CategoryUpdate", None, ss_feed.parse_categories)
		brands = self._fetch("BrandUpdate", None, ss_feed.parse_brands)
		stats = {}
		out = self._fetch("DailyItemUpdate", {"LastUpdate": last_update, "LastItem": "-1"},
			lambda p: store.load_catalog(
				ss_feed.iter_catalog(p, brands=brands, department_names=names,
					caliber_slots=slots, stats=stats, present=present),
				distributor=self.code, mode=mode, exclude_cols=EXCLUDE_COLS,
				insert_only_cols=INSERT_ONLY_COLS, min_rows=min_rows),
			timeout=timeout)
		if out.get("lock_skipped"):
			return out
		if out.get("created"):
			# Rows born in this load carry feed-time qty and cost; replaying the quantity
			# window makes the quantity lane authoritative again.
			set_qty_cursor(self.code, _fmt_onhand_since(started - REWIND), only_if_earlier=True)
		out["rejected"] = stats.get("rejected", 0)
		return out

	def _snapshot(self, run) -> dict:
		# lazy: the item numbers are held as a set, about 60k strings; stage them in a temp table if the catalog grows far past that
		present = set()
		out = self._load_items(run, mode="snapshot", last_update="1/1/1990",
			min_rows=SNAPSHOT_MIN_ROWS, present=present, timeout=TIMEOUT_SNAPSHOT)
		if out.get("lock_skipped"):
			return out
		run.state["snapshot_loaded"] = True
		live = set(frappe.db.sql_list(f"""SELECT sku FROM dc_offer WHERE distributor = %s
			AND NOT discontinued AND NOT {store.deleted_sql()}""", (self.code,)))
		missing = sorted(live - present)
		if missing:
			if len(missing) > len(live) * DISAPPEARED_FRACTION:
				raise ValueError(f"disappeared diff too large: {len(missing)} of {len(live)} live "
					f"rows vanished from the snapshot (more than {DISAPPEARED_FRACTION:.0%}) - "
					"refusing to flag")
			d = store.apply_deleted(missing, distributor=self.code, status_flag="Discontinued")
			if d.get("lock_skipped"):
				raise RuntimeError("write lock busy while flagging vanished rows - the next snapshot retries")
			out["disappeared"] = d["flagged"]
		return out

	def _catalog(self, run) -> dict:
		since = (run.state["delta_base"] - timedelta(days=1)).date()
		return self._load_items(run, mode="delta", last_update=_fmt_date(since), min_rows=0,
			present=None, timeout=TIMEOUT_SMALL)

	def _text(self, run) -> dict:
		base = run.state.get("text_base")
		d = (base - timedelta(days=1)).date() if base else date(1990, 1, 1)
		return self._fetch("ListNewText", {"DateFrom": _fmt_date(d)},
			lambda p: store.apply_text(ss_feed.iter_text_rows(p), distributor=self.code),
			timeout=TIMEOUT_SNAPSHOT)

	def _quantity(self, run) -> dict:
		cursor = qty_cursor(self.code)
		since = cursor or _fmt_onhand_since(datetime.now(_CENTRAL) - timedelta(hours=1))
		servertime, triples = self._fetch("IncrementalOnhandUpdate", {"SinceDateTime": since},
			ss_feed.parse_onhand_incremental)
		if not servertime and not triples:
			# An idle buffer holds the cursor; the full refresh runs at most every 30 minutes.
			if not sync.is_due(run.source("onhand_full"), ONHAND_FULL_STALE, run.now):
				return {"updated": 0, "total": 0, "mode": "idle"}
			return self._quantity_full(run)
		out = store.apply_quantity(triples, distributor=self.code)
		if out.get("lock_skipped"):
			return out  # the cursor does not advance: the window replays on the next run
		set_qty_cursor(self.code, servertime, expect=cursor)
		out["mode"] = "incremental"
		return out

	def _quantity_full(self, run) -> dict:
		started = store.utc_now()
		out = self._fetch("OnhandUpdate", None,
			lambda p: store.apply_quantity(ss_feed.iter_onhand_full(p), distributor=self.code),
			timeout=TIMEOUT_ONHAND_FULL)
		if out.get("lock_skipped"):
			return out
		# Its own gate row; the full refresh cannot supply a cursor.
		sync.record_run(run.source("onhand_full"), "success", started,
			rows_total=out.get("total") or 0)
		out["mode"] = "full"
		return out


DRIVER = sync.register(SsDriver())
