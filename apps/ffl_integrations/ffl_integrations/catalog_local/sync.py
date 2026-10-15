"""Local catalog sync framework — a port of the Data Service's driver base and
lane runner to Frappe jobs.

A house is one ``Driver`` listing its ``Lane``s in execution order. Each lane owns
its gate and its fetch → parse → write body. Gates stay OUTSIDE the stage (a gate
skip records nothing; a gate that raises records a failed run of its lane in
``run_lane``/``run_house`` and is skipped by ``tick``); bodies run inside
``stage`` (a failure records a failed ``dc_sync_run`` row and the next lane still
runs). A body that hits the house's
write lock returns ``store.LOCK_SKIPPED`` and records no row either.

Frozen names: ``dc_sync_run.source`` is ``f"{code}_{lane}"`` — gates, ``health()``
and ops read those strings, so a lane is never renamed.

Scheduling: ``tick`` (cron ``*/15``) is a no-op unless the site reads the Local
catalog. It enqueues one ``run_lane`` job per due lane of every configured house on
the dedicated ``catalog`` queue — explicit timeout, ``job_id`` + ``deduplicate`` —
and at most ONE heavy lane in flight across all houses: a heavy job still queued
or running holds the slot, so a long snapshot never has a second one queued
behind it ahead of the 15-minute quantity lanes. Which house gets a free slot is
``_fair_order`` (the oldest heavy attempt first). A lane that ``follows`` another
runs in that lane's job, straight after it.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import ClassVar

import frappe

from ffl_integrations.catalog_local import QUEUE, is_local, store

QUANTITY_TIMEOUT_SECONDS = 300
HEAVY_TIMEOUT_SECONDS = 1500
DEFAULT_FEED_STALE_SECONDS = 21600
WORKER_ALERT_KEY = "catalog_local_worker_missing"


def _log():
	return frappe.logger("catalog_local", allow_site=True)


class FeedUnavailable(RuntimeError):
	"""The distributor could not be reached or refused us."""


class FeedFormatError(ValueError):
	"""Reachable, but the response was not usable (error payload, bad shape)."""


class FeedAuthError(FeedUnavailable):
	"""The distributor rejected our credentials (or our IP)."""


@dataclass(frozen=True)
class Tick:
	now: datetime | None = None   # None: stamp the run's own start (UTC, naive)
	# A forced run from bench: every gate open AND an overridable guard (the restrictions shrink guard) overridden
	force: bool = False
	# Sync now (the distributor hub): every gate open; no guard is touched
	skip_schedule: bool = False

	@property
	def gates_open(self) -> bool:
		"""Should a lane skip its schedule? A forced run and Sync now both do."""
		return self.force or self.skip_schedule


@dataclass
class Run:
	driver: Driver
	tick: Tick
	now: datetime
	state: dict = field(default_factory=dict)  # lane-to-lane scratch

	def source(self, lane: str) -> str:
		return f"{self.driver.code}_{lane}"

	def due(self, lane: str, stale: timedelta) -> bool:
		"""The usual gate: last SUCCESS older than ``stale``; a forced run or Sync now skips it."""
		return self.tick.gates_open or is_due(self.source(lane), stale, self.now)


@dataclass(frozen=True)
class Lane:
	name: str                          # never rename: it IS the dc_sync_run source suffix
	gate: Callable[[Run], bool]        # False → no stage, no row
	run: Callable[[Run], dict]         # the stage body
	heavy: bool = False                # one heavy lane per tick across houses
	timeout: int = QUANTITY_TIMEOUT_SECONDS
	# The lane whose job this one rides: it runs straight after that lane, in the
	# same job and through its own gate, unless that lane failed; the tick does not
	# queue it on its own while that lane is queued or running.
	follows: str = ""


class Driver:
	"""One distributor house. Subclasses set ``code`` and return their lanes."""
	code: ClassVar[str]
	label: ClassVar[str] = ""
	# ops staleness budget per lane, seconds (DEFAULT_FEED_STALE_SECONDS otherwise)
	feed_stale: ClassVar[dict] = {"quantity": 5400}

	def lanes(self) -> tuple[Lane, ...]:
		raise NotImplementedError

	def lane(self, name: str) -> Lane | None:
		return next((ln for ln in self.lanes() if ln.name == name), None)

	def configured(self) -> bool:
		"""Should the tick sync this house? Default: an enabled Distributor row
		buys through it. A driver with credentials to check narrows this."""
		return bool(frappe.db.exists("Distributor", {"provider": self.code, "enabled": 1}))

	def stale_after(self, lane: str) -> int:
		return int(self.feed_stale.get(lane, DEFAULT_FEED_STALE_SECONDS))


# House code → driver. Real houses register when ``catalog_local.drivers`` is
# imported (``load_drivers``); tests register synthetic ones.
DRIVERS: dict[str, Driver] = {}


def load_drivers() -> dict:
	"""Import the shipped drivers (each registers itself) → ``DRIVERS``. Every
	entry point calls it first: a worker or web process may not have imported
	them yet. Idempotent (a module imports once)."""
	import ffl_integrations.catalog_local.drivers  # noqa: F401 — registers

	return DRIVERS


def register(driver: Driver) -> Driver:
	DRIVERS[driver.code] = driver
	return driver


def unregister(code: str) -> None:
	DRIVERS.pop(code, None)


# ------------------------------------------------------------------ run records

def last_success(source: str):
	return frappe.db.sql("""SELECT MAX(finished_at) FROM dc_sync_run
		WHERE source = %s AND status = 'success'""", (source,))[0][0]


def is_due(source: str, stale: timedelta, now: datetime) -> bool:
	"""Fail OPEN on a read error: the stage then records the honest failure."""
	try:
		last = last_success(source)
	except Exception:  # noqa: BLE001
		_log().exception("catalog gate read failed for %s; treating as due", source)
		return True
	return last is None or now - last > stale


def record_run(source: str, status: str, started: datetime, *, rows_total: int = 0,
		error: BaseException | None = None, note: str | None = None) -> None:
	"""One dc_sync_run row, committed. ``error_msg`` keeps the exception text for
	diagnosis on this site; only the class name ever leaves it (ops_history). A
	success may carry a note (for example a guard a forced run overrode)."""
	frappe.db.sql("""INSERT INTO dc_sync_run (source, status, started_at, finished_at,
			rows_total, error_class, error_msg) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
		(source, status, started, store.utc_now(), int(rows_total or 0),
			type(error).__name__ if error else "",
			f"{type(error).__name__}: {error}"[:2000] if error else (note[:2000] if note else None)))
	frappe.db.commit()


def mark_attempt(code: str, lane: str) -> None:
	"""Stamp the lane's latest attempt (success or not) in ``dc_meta.lane_attempts``
	— the tick orders houses by it, so a failing heavy lane goes to the back."""
	frappe.db.sql("INSERT IGNORE INTO dc_meta (distributor) VALUES (%s)", (code,))
	frappe.db.sql("""UPDATE dc_meta SET lane_attempts = JSON_SET(
			COALESCE(NULLIF(lane_attempts, ''), '{}'), CONCAT('$."', %s, '"'), %s)
		WHERE distributor = %s""", (lane, store.utc_now().isoformat(), code))
	frappe.db.commit()


def clear_attempt(code: str, lane: str) -> None:
	"""Drop the lane's attempt stamp: the attempt ended without a verdict (a write
	lock skip records no run), so a gate that reads attempts must not take it for
	a run that died."""
	frappe.db.sql("""UPDATE dc_meta SET lane_attempts = JSON_REMOVE(lane_attempts,
			CONCAT('$."', %s, '"'))
		WHERE distributor = %s AND COALESCE(lane_attempts, '') != ''""", (lane, code))
	frappe.db.commit()


def lane_attempts() -> dict:
	"""``{house: {lane: naive UTC datetime}}`` from dc_meta."""
	out = {}
	for code, raw in frappe.db.sql("SELECT distributor, lane_attempts FROM dc_meta"):
		try:
			out[code] = {k: datetime.fromisoformat(v) for k, v in json.loads(raw or "{}").items()}
		except (TypeError, ValueError):
			out[code] = {}
	return out


def _reconnect() -> None:
	"""The connection died under a lane: open a fresh one so the failure can
	still be recorded and the next lane can run — at READ COMMITTED again when
	the lanes run inside ``store.read_committed`` (a new session starts at the
	server default)."""
	try:
		frappe.db.close()
	except Exception:  # noqa: BLE001 — it is already gone
		pass
	frappe.db.connect()
	store.reapply_isolation()


def stage(source: str, fn: Callable[[], dict], failed: list | None = None):
	"""Run one lane body, record a dc_sync_run row either way, never raise.
	``failed`` (when given) collects the sources that failed. A rollback that
	fails (the connection is gone) reconnects before recording."""
	started = store.utc_now()
	try:
		result = fn() or {}
	except Exception as exc:  # noqa: BLE001 — stage isolation is the contract
		_log().exception("%s failed", source)
		try:
			frappe.db.rollback()
		except Exception:  # noqa: BLE001
			_log().exception("%s: rollback failed; reconnecting", source)
			try:
				_reconnect()
			except Exception:  # noqa: BLE001
				_log().exception("%s: reconnect failed", source)
		try:
			record_run(source, "failed", started, error=exc)
		except Exception:  # noqa: BLE001 — never raise out of a stage
			_log().exception("%s: could not record the failed run", source)
		if failed is not None:
			failed.append(source)
		return None
	if result.get("lock_skipped"):
		_log().info("%s skipped: dc_write lock busy", source)
		return result
	try:
		record_run(source, "success", started,
			rows_total=result.get("total") or result.get("rows_written") or 0,
			note=result.get("warning"))
	except Exception:  # noqa: BLE001 — the data is committed; the record is diagnosis
		_log().exception("%s: could not record the successful run", source)
	return result


# ------------------------------------------------------------------ entry points

NOT_DUE = {"skipped": "not_due"}


def _gate_and_stage(run: Run, ln: Lane, failed: list | None = None):
	"""One lane: gate, attempt stamp, stage → the stage result, or ``NOT_DUE``.
	A gate that raises is a failed run of its lane (recorded through ``stage``);
	a stamp that fails is logged — it only orders the next tick."""
	source = run.source(ln.name)
	try:
		due = ln.gate(run)
	except Exception as exc:  # noqa: BLE001 — one broken gate must not stop the house
		def reraise(exc=exc):
			raise exc

		return stage(source, reraise, failed=failed)
	if not due:
		return NOT_DUE
	try:
		mark_attempt(run.driver.code, ln.name)
	except Exception:  # noqa: BLE001
		_log().exception("catalog %s: could not stamp the attempt", source)
		try:
			frappe.db.rollback()
		except Exception:  # noqa: BLE001 — stage reconnects if the connection is gone
			pass
	return stage(source, lambda: ln.run(run), failed=failed)


def _with_followers(run: Run, ln: Lane, failed: list | None = None) -> dict:
	"""``ln``, then each lane that ``follows`` it, in the same job and through the
	follower's own gate - unless ``ln`` failed (a failed leader takes no follower
	with it). -> ``{lane name: stage result, None (failed) or NOT_DUE}``."""
	out = {ln.name: _gate_and_stage(run, ln, failed)}
	if out[ln.name] is None:
		return out
	for f in run.driver.lanes():
		if f.follows == ln.name:
			out[f.name] = _gate_and_stage(run, f, failed)
	return out


def run_lane(code: str, lane: str, force: bool = False, skip_schedule: bool = False):
	"""The catalog-queue job: one lane of one house, then the lanes that follow it
	(Lane.follows), at READ COMMITTED. The gate is checked again here (the job may
	have waited in the queue). ``skip_schedule`` (Sync now) opens the gates and nothing else; only ``force`` overrides a guard."""
	driver = load_drivers().get(code)
	ln = driver.lane(lane) if driver else None
	if ln is None:
		_log().warning("catalog lane %s_%s is not registered; job dropped", code, lane)
		return None
	run = Run(driver, Tick(force=force, skip_schedule=skip_schedule), store.utc_now())
	with store.read_committed():
		out = _with_followers(run, ln)[ln.name]
	return dict(NOT_DUE) if out is NOT_DUE else out


def run_house(code: str, *, force: bool = False, skip_schedule: bool = False,
		now: datetime | None = None) -> dict:
	"""Every due lane of one house, in order, in this process (tests; a run from bench).
	``{lane: result | None, "_failed": [sources]}`` (None: not due)."""
	driver = load_drivers()[code]
	run = Run(driver, Tick(now=now, force=force, skip_schedule=skip_schedule),
		now or store.utc_now())
	result: dict = {**{ln.name: None for ln in driver.lanes()}, "_failed": []}
	with store.read_committed():
		for ln in driver.lanes():
			if ln.follows:
				continue  # runs in its leader's turn
			for name, out in _with_followers(run, ln, result["_failed"]).items():
				result[name] = None if out is NOT_DUE else out
	return result


def job_id(code: str, lane: str) -> str:
	return f"catalog_local:{code}:{lane}"


def sync_now(code: str) -> dict:
	"""Sync now (the distributor hub): queue every lane of one house on the catalog queue with its schedule skipped (``skip_schedule``). It never overrides a guard: a short, truncated or shrunk feed file is refused exactly as on a scheduled run — only a forced run from bench may load a shrunk restrictions file. A lane that follows another rides that lane's job. Each job keeps the tick's job id, so a lane already queued or running is not queued twice. -> ``{enqueued: [sources], deduplicated: [sources], worker_running}``."""
	driver = load_drivers().get(code)
	if driver is None:
		frappe.throw(frappe._("No local catalog driver serves '{0}'.").format(code))
	if not driver.configured():
		frappe.throw(frappe._("{0} is not set up for the local catalog: enable its Distributor and enter its feed credentials on its settings page.").format(driver.label or code))
	enqueued, deduplicated = [], []
	for ln in driver.lanes():
		if ln.follows:
			continue  # rides its leader's job
		job = frappe.enqueue("ffl_integrations.catalog_local.sync.run_lane", queue=QUEUE,
			timeout=ln.timeout, job_id=job_id(code, ln.name), deduplicate=True,
			code=code, lane=ln.name, skip_schedule=True)
		(deduplicated if job is None else enqueued).append(f"{code}_{ln.name}")
	return {"enqueued": enqueued, "deduplicated": deduplicated,
		"worker_running": worker_running()}


def lane_health(code: str) -> dict | None:
	"""For the distributor hub: each lane's last success (time, rows) and its latest run's outcome, plus the house's offer counts. None when no local driver serves the house. Times are this site's time zone (dc_sync_run keeps UTC)."""
	from frappe.utils import convert_utc_to_system_timezone

	driver = load_drivers().get(code)
	if driver is None:
		return None
	sources = tuple(f"{code}_{ln.name}" for ln in driver.lanes())
	ok = {source: (fin, rows) for source, fin, rows in frappe.db.sql("""SELECT r.source, r.finished_at, r.rows_total FROM dc_sync_run r
		JOIN (SELECT MAX(id) AS id FROM dc_sync_run WHERE source IN %(s)s AND status = 'success' GROUP BY source) m ON m.id = r.id""", {"s": sources})}
	latest = {source: (status, err) for source, status, err in frappe.db.sql("""SELECT r.source, r.status, r.error_class FROM dc_sync_run r
		JOIN (SELECT MAX(id) AS id FROM dc_sync_run WHERE source IN %(s)s GROUP BY source) m ON m.id = r.id""", {"s": sources})}
	total, active = frappe.db.sql(f"""SELECT COUNT(*), COALESCE(SUM(NOT discontinued AND NOT {store.deleted_sql()}), 0)
		FROM dc_offer WHERE distributor = %s""", (code,))[0]
	lanes = []
	for ln in driver.lanes():
		source = f"{code}_{ln.name}"
		fin, rows = ok.get(source, (None, 0))
		status, err = latest.get(source, (None, ""))
		lanes.append({
			"lane": ln.name,
			"last_success_at": convert_utc_to_system_timezone(fin).strftime("%Y-%m-%d %H:%M:%S") if fin else None,
			"rows": int(rows or 0),
			"last_status": status or None,
			"error_class": err or "",
		})
	return {"lanes": lanes, "offers_total": int(total or 0), "offers_active": int(active or 0)}


def tick() -> dict:
	"""Scheduler ``*/15``. A no-op unless Local; otherwise enqueue each due lane
	of each configured house on the catalog queue (one heavy lane per tick)."""
	if not is_local():
		return {"skipped": "not_local"}
	load_drivers()
	if not worker_running():
		from ffl_integrations.notifications import notify_admins

		notify_admins("Catalog worker not running",
			"This site reads its distributor catalog locally (Data Service Settings › "
			"Catalog source = Local), but no worker listens on the 'catalog' queue, so "
			"the catalog is not syncing. Start the queue-catalog service (deploy compose) "
			"or a `bench worker --queue catalog`.", dedup_key=WORKER_ALERT_KEY,
			window_minutes=360)
	now = store.utc_now()
	enqueued, deduplicated, yielded = [], [], []
	# A heavy job still queued or running holds the slot: no other house's heavy
	# lane starts beside it. Who gets a free slot is _fair_order's call.
	busy = _heavy_in_flight()
	heavy_taken = bool(busy)
	for code in _fair_order():
		driver = DRIVERS[code]
		try:
			if not driver.configured():
				continue
		except Exception:  # noqa: BLE001 — one house's config read must not stop the tick
			_log().exception("catalog house %s: configured() failed", code)
			continue
		run = Run(driver, Tick(now=now), now)
		for ln in driver.lanes():
			leader = run.source(ln.follows) if ln.follows else ""
			if leader and (leader in busy or leader in enqueued or leader in deduplicated):
				continue  # it rides its leader's job
			try:
				if not ln.gate(run):
					continue
			except Exception:  # noqa: BLE001 — a broken gate skips its lane, not the tick
				_log().exception("catalog gate %s raised; lane skipped", run.source(ln.name))
				continue
			if ln.heavy and run.source(ln.name) in busy:
				deduplicated.append(run.source(ln.name))
				continue
			if ln.heavy and heavy_taken:
				yielded.append(run.source(ln.name))
				continue
			try:
				job = frappe.enqueue("ffl_integrations.catalog_local.sync.run_lane", queue=QUEUE,
					timeout=ln.timeout, job_id=job_id(code, ln.name), deduplicate=True,
					code=code, lane=ln.name)
			except Exception as exc:  # noqa: BLE001 — queue undeclared / Redis down
				from ffl_integrations.notifications import safe_log_error

				safe_log_error("Catalog lane could not be queued",
					f"{run.source(ln.name)}: {type(exc).__name__}: {exc}. Run `bench migrate` "
					"(it declares the 'catalog' queue) and check Redis.")
				return {"enqueued": enqueued, "deduplicated": deduplicated, "yielded": yielded,
					"error": type(exc).__name__}
			if job is None:
				# Already queued or running (it went in since _heavy_in_flight
				# looked): nothing new, but a heavy one still holds the slot.
				deduplicated.append(run.source(ln.name))
				heavy_taken = heavy_taken or ln.heavy
				continue
			enqueued.append(run.source(ln.name))
			heavy_taken = heavy_taken or ln.heavy
	if yielded:
		_log().info("catalog heavy lanes yielding to the next tick: %s", ", ".join(yielded))
	return {"enqueued": enqueued, "deduplicated": deduplicated, "yielded": yielded}


def _heavy_in_flight() -> set:
	"""Sources of the heavy lanes whose job is queued or running now. Unreadable
	(Redis down) = none: the enqueue below then fails and says so."""
	from frappe.utils.background_jobs import is_job_enqueued

	busy = set()
	for code, driver in DRIVERS.items():
		try:
			for ln in driver.lanes():
				if ln.heavy and is_job_enqueued(job_id(code, ln.name)):
					busy.add(f"{code}_{ln.name}")
		except Exception:  # noqa: BLE001
			_log().exception("catalog house %s: could not read its queued jobs", code)
	return busy


def _fair_order() -> list:
	"""House codes, the one whose heavy lane was attempted longest ago (or never)
	first — so a house whose heavy lane keeps failing cannot hold the one heavy
	slot every tick."""
	attempts = lane_attempts()

	def oldest_heavy(code):
		heavy = [ln.name for ln in DRIVERS[code].lanes() if ln.heavy]
		seen = [attempts.get(code, {}).get(name) for name in heavy]
		return min((t or datetime.min for t in seen), default=datetime.min)

	return sorted(DRIVERS, key=lambda code: (oldest_heavy(code), code))


# ------------------------------------------------------------------ worker health

def worker_running() -> bool:
	"""Does any RQ worker listen on the catalog queue? False when the queue is not
	even declared (common_site_config ``workers``; ``schema.ensure`` adds it)."""
	from frappe.utils.background_jobs import get_queue, get_queues_timeout, get_workers

	try:
		if QUEUE not in get_queues_timeout():
			return False
		return bool(get_workers(get_queue(QUEUE)))
	except Exception:  # noqa: BLE001 — Redis down reads as "not running"
		return False


def status() -> dict:
	"""For the distributor hub: is the catalog local, and is its worker up?
	``warning`` is the line to show (empty when there is nothing to say)."""
	if not is_local():
		return {"local": False, "worker_running": None, "warning": ""}
	running = worker_running()
	return {"local": True, "worker_running": running,
		"warning": "" if running else frappe._(
			"Catalog worker not running: the local catalog is not syncing. Start the "
			"queue-catalog service (or a worker on the 'catalog' queue).")}
