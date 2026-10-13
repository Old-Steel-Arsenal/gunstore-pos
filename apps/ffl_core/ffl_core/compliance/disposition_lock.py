"""DB-level idempotency for FFL Disposition minting.

Two concurrent ``form4473.completed`` deliveries (a FastBound retry racing the
original) could both pass the read-then-write existence check and double-mint
the same ``(fastbound_id, serial)`` Retail Sale disposition
Serialize the check-then-insert with a MariaDB advisory lock so
exactly ONE row is booked.

Chosen over a DB unique constraint: ``manager_override_4473``
mints rows with an EMPTY ``fastbound_id`` and the same serial can legitimately
recur (buyback resale, a repeat override), so a naive composite unique index
would misfire on the ``''``-collision (Frappe Data fields store ``""``, not NULL,
so empty keys would clash). ``GET_LOCK`` needs no schema change, so it is the
upgrade-safest option (b). The lock degrades to a plain check-insert if
``GET_LOCK`` is unavailable — never worse than the historical read-then-write —
but never silently: acquisition is retried once, and every degrade logs a
warning with the reason (the 2026-07-17 nightly showed a swallowed transient
failure reopening the double-mint window with no trace in any log).
"""
from __future__ import annotations

import hashlib
from typing import Callable

import frappe

from ffl_core.utils import commit as safe_commit

_LOCK_TIMEOUT = 10  # seconds a racing delivery waits for the winner to finish


def _lock_name(dedup_key: str) -> str:
	# MariaDB GET_LOCK names are capped at 64 chars — hash to a fixed, safe token.
	return "ffldisp:" + hashlib.sha1(dedup_key.encode("utf-8")).hexdigest()


def _logger():
	return frappe.logger("ffl_core.disposition_lock", allow_site=True)


def _get_lock(name: str) -> bool:
	try:
		got = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (name, _LOCK_TIMEOUT))[0][0]
	except Exception:  # noqa: BLE001 — the lock is an optimisation, not a prerequisite
		_logger().warning(
			f"GET_LOCK({name}) raised — degrading to unserialized check-insert",
			exc_info=True)
		return False
	if not got:
		# 0 = wait timed out; NULL = server-side error (OOM / killed thread)
		_logger().warning(
			f"GET_LOCK({name}) returned {got!r} — degrading to unserialized check-insert")
	return bool(got)


def _release_lock(name: str) -> None:
	try:
		frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
	except Exception:  # noqa: BLE001
		pass


def book_disposition_once(*, dedup_key: str, exists_filters: dict,
		insert_fn: Callable[[], str]) -> str | None:
	"""Return the name of the ONE FFL Disposition for ``dedup_key``, calling
	``insert_fn()`` to create it only if ``exists_filters`` matches none — the whole
	check-then-insert serialized by an advisory lock so a concurrent delivery
	can't double-mint.

	``insert_fn()`` must build + insert (+ submit as appropriate) the disposition
	and return its name; it is called at most once, only when no matching row
	exists. ``exists_filters`` is the idempotency identity (e.g.
	``{"fastbound_id": ..., "serial": ...}``). Returns the pre-existing name if
	already booked, else the newly-created name.
	"""
	name = _lock_name(dedup_key)
	# One transient GET_LOCK failure must not reopen the double-mint window —
	# retry once (a genuine outage fails twice and still degrades, logged).
	got = _get_lock(name) or _get_lock(name)
	try:
		if got:
			# The winner commits before releasing the lock; reset our MVCC snapshot
			# (a real commit in prod and in the concurrent-delivery threads; a no-op
			# inside a single test transaction, where same-transaction reads already
			# see prior inserts) so this re-check sees that committed row and skips.
			safe_commit()
		existing = frappe.db.get_value("FFL Disposition", exists_filters, "name")
		if existing:
			return existing
		new_name = insert_fn()
		safe_commit()  # publish so the next lock-waiter's re-check sees it
		return new_name
	finally:
		if got:
			_release_lock(name)
