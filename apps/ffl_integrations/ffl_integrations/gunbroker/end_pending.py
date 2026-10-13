"""Chasing down the listings we asked GunBroker to end but could not confirm.

Ending a listing is a delete followed by a read-back, because GunBroker returns
200 for things it did not do. When the read-back cannot be obtained — a timeout,
a 5xx, GunBroker having a bad afternoon — the gun is left in END_PENDING: we
believe it is off sale and we have no proof.

That state is the dangerous one, and it is dangerous in the direction of a
double sale. The gun has usually left the shelf by the time this runs (it is
ended because it sold at the counter, or on the web store), while its GunBroker
listing may still be buyable. So it is checked every fifteen minutes rather than
waiting up to an hour for the sweep. Everything else about the listing state
machine stays with the sweep; this is the one state that cannot afford the wait.

Two rules make this safe to run on a single look, and both are the opposite of
what the sweep does:

**One inactive reading is enough here.** The sweep will not conclude "ended"
from one look, because a fixed-price listing goes briefly inactive every thirty
days while GunBroker relists it, and calling that ENDED would mark a live
listing dead. But an END_PENDING gun is one we ASKED to end — inactive is the
answer we were waiting for, not an ambiguous one.

**Sold beats ended.** If the read-back shows a buyer, the delete lost a race
with a purchase, and marking it ENDED would bury a sale nobody has fulfilled.
That case is left exactly where it is for the sweep and the order poll to pick
up, which is what they are for.
"""
from __future__ import annotations

import frappe
from frappe import _

from ffl_core.utils import commit as safe_commit
from ffl_core.utils import single_flag
from ffl_integrations.gunbroker import listings
from ffl_integrations.gunbroker.client import (
	GunBrokerClient,
	GunBrokerError,
	get_field,
)
from ffl_integrations.integration_gate import gunbroker_enabled
from ffl_integrations.notifications import notify_admins, safe_log_error

SETTINGS_DOCTYPE = "GunBroker Settings"

# One tick's worth. An account with more unconfirmed ends than this has a
# problem the sweep and the alerts are already shouting about, and grinding
# through all of them on a fifteen-minute timer would not help.
BATCH = 100

# Stable across ticks on purpose: it is what stops a slow round having a second
# one queued behind it.
END_PENDING_JOB_ID = "gb-end-pending"


def poll_end_pending() -> dict:
	"""Cron entry (every 15 minutes). Gate, then enqueue; never raises.

	Nothing outbound happens on the tick. A round is up to ``BATCH`` separate
	``GET /Items/{id}`` calls, and a Cron scheduled job runs on the DEFAULT
	queue with its 300-second budget — a round that gets cut off part way has
	no resume point. It goes on the long queue instead, deduplicated on a
	stable job_id so a slow round cannot have a second one stacked behind it.
	"""
	summary = {"enqueued": False, "dependency_missing": False}
	try:
		if not gunbroker_enabled():
			return summary
		if not single_flag(SETTINGS_DOCTYPE, "auto_sync_enabled", default=1):
			return summary
		if _sold_predicate() is None:
			summary["dependency_missing"] = True
			return summary
		frappe.enqueue(
			"ffl_integrations.gunbroker.end_pending._run_end_pending",
			queue="long", job_id=END_PENDING_JOB_ID, deduplicate=True)
		summary["enqueued"] = True
	except Exception:  # noqa: BLE001 — a gate must not break the tick
		safe_log_error("GunBroker end-pending poll could not be enqueued",
			frappe.get_traceback())
	return summary


def _run_end_pending() -> dict:
	"""One round: confirm what we can, and leave everything else pending."""
	summary = {"considered": 0, "confirmed": 0, "still_pending": 0,
		"dependency_missing": False}
	try:
		if _sold_predicate() is None:
			# Checked again here, not only at the gate: this is the function
			# that reports what the round did, and "0 confirmed" must never be
			# the way a missing dependency shows up.
			summary["dependency_missing"] = True
			return summary
		rows = end_pending_serials()
		summary["considered"] = len(rows)
		if not rows:
			return summary
		client = GunBrokerClient.from_settings()
		for row in rows:
			if confirm_one(row, client=client):
				summary["confirmed"] += 1
			else:
				summary["still_pending"] += 1
		safe_commit()
	except Exception:  # noqa: BLE001 — a background job must not die noisily
		safe_log_error("GunBroker end-pending poll failed", frappe.get_traceback())
	return summary


def _sold_predicate():
	"""PR-2's "has this listing got a buyer?" test, or None if it is not here.

	Sharing that test with the sweep is right: GunBroker signals a sale in
	several undocumented ways, and the union of them is a fact this integration
	should hold in exactly one place. But it lives in a module a LATER pull
	request adds, and until that one is merged the import raises — inside the
	per-serial loop, where the blanket handler swallows it. The visible result
	was an Error Log every fifteen minutes and a summary reading zero
	confirmed, which is also what a healthy quiet account looks like: a
	double-sale safety net that had never once run and said nothing about it.

	So the absence is checked for up front, said out loud once a day, and
	reported in the summary.
	"""
	try:
		from ffl_integrations.gunbroker.doc_events import gb_item_sold
	except ImportError:
		notify_admins(
			_("GunBroker end-pending confirmation is not running"),
			_("The fifteen-minute job that confirms listings we asked GunBroker "
				"to end cannot start: ffl_integrations.gunbroker.doc_events is "
				"not installed on this site, and it owns the single test for "
				"whether a listing has a buyer.\n\nWhile this is missing, a gun "
				"whose listing we could not confirm ended stays buyable on "
				"GunBroker with nothing chasing it — and it is normally a gun "
				"that has already left the shelf. End those listings by hand on "
				"the GunBroker website until the deployment is complete."),
			dedup_key="gb_end_pending_dependency_missing", window_minutes=1440)
		return None
	return gb_item_sold


def end_pending_serials(limit: int = BATCH) -> list[dict]:
	"""Guns whose end we asked for and never confirmed.

	Both columns are required. ``gb_end_pending`` with no item number is a gun
	the manual strategy flagged for a human — there is nothing to ask GunBroker
	about, and asking about an empty id would be a request for item "".
	"""
	return frappe.db.sql(
		"""
		SELECT name, gb_item_id, gb_item_history
		FROM `tabSerial No`
		WHERE COALESCE(gb_end_pending, 0) = 1
		  AND COALESCE(gb_item_id, '') != ''
		ORDER BY COALESCE(gb_synced_at, creation) ASC
		LIMIT %(limit)s
		""",
		{"limit": int(limit)}, as_dict=True)


def confirm_one(row: dict, *, client: GunBrokerClient) -> bool:
	"""Ask GunBroker about one listing. True when it is now confirmed ended.

	Anything other than a clear "inactive, unsold" answer leaves the flag up.
	Not being able to confirm is the state we are already in, and downgrading a
	failed question into an answer is the one move that turns this job from a
	safety net into a hazard.
	"""
	serial_no = row.get("name")
	item_id = str(row.get("gb_item_id") or "")
	try:
		verdict = client.item_probe(item_id)
	except GunBrokerError:
		safe_log_error(f"GunBroker end-pending probe failed for {serial_no}",
			frappe.get_traceback())
		return False
	if verdict.get("state") != "ok":
		# `gone` is not confirmation either, however tempting: it is also what a
		# bad item number looks like, and the sweep owns that judgement — it has
		# the two-round rule and the SKU scans this job deliberately does not.
		return False
	item = verdict.get("item") or {}
	if get_field(item, "isActive", "IsActive") is not False:
		return False
	if _item_sold(item):
		# The delete lost a race with a buyer. Marking this ENDED would bury a
		# sale nobody has fulfilled; the order poll and the sweep handle it.
		return False
	if listings._numeric_id(get_field(item, "relistedAsItemID", "RelistedAsItemID")):
		# Inactive because GunBroker MOVED the listing, not because it is over.
		# Confirming here clears `gb_item_id`, which drops the gun out of the
		# sweep's candidate set and leaves the replacement live with nothing
		# pointing at it. Leaving the flag up costs another quarter of an hour of
		# this job saying "not yet" and hands the case to the sweep, which has the
		# SKU scans and `_recheck_after_end` for exactly this shape.
		return False
	listings.confirm_ended(serial_no, item_id, row)
	return True


def _item_sold(item: dict) -> bool:
	"""Has this listing got a buyer? One test, defined once, for the channel.

	Through the sweep's own predicate rather than a second reading of the same
	four fields — GunBroker signals a sale in several ways and none of them is
	documented, so the union of them is a fact this integration should hold in
	exactly one place.
	"""
	predicate = _sold_predicate()
	if predicate is None:
		# Not reachable through the poll, which checks and returns before it
		# gets here. A direct caller gets the answer that changes nothing:
		# "there may be a buyer" leaves the listing exactly where it was.
		return True
	return predicate(item)
