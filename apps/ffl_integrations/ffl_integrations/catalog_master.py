"""The catalog master's category tree in the POS (SPEC M3b, catalog master M3).

The Data Service's tree (``/v1/catalog/categories``) is the one source of category names,
parents and order. This module mirrors it into ``Catalog Category`` rows and links each
node to the Item Group it files as; ``ffl_woo_sync.woocommerce.categories`` links
the same nodes to each store's product categories. Links are by id: after the
first link a node is never re-found by name — a rename renames in place, a move
re-parents in place, a retired node is left where it is (never deleted).

Products: a catalog Item (its UPC is a product in the master) files under the
master's category — ``classify`` at promotion, ``plan_item`` for the change feed and
``m3_resync``. A non-catalog Item keeps its Item Group, and the group's node gives
its website category. The title / class rules of ``ffl_core.accessory_groups`` are
only the fallback when the master has no answer (unreachable, or no category yet).

Never automatic: re-grouping a serialized Item, moving an Item into or out of a
firearm Item Group, changing ``is_firearm`` on an Item that exists, creating or
re-parenting an Item Group under Firearm. Those are reported for the owner.

The mirror runs every tick in the mode set on Data Service Settings › Categories:
Off, Report only (the default — lists what it would do, changes nothing but its
own mirror rows and links to groups that already exist) or Apply.

Each half follows its own capability (``data_service.capabilities``): the tree
mirror needs ``category_tree`` (only the Data Service has a tree); following the
product changes (Apply) needs only ``catalog``, so a Local catalog is followed
too. With the tree off the tick clears the mirror's own stale report once and
writes nothing more.
"""
from __future__ import annotations

import hashlib
import json

import frappe
from frappe.utils import cint, now_datetime

from ffl_core import catalog_tree
from ffl_core.catalog_tree import (
	DOCTYPE, ROOT_GROUP, _is_group_node, apply_item, category_of, group_for, group_for_product,
	is_firearm_group, legal_issue, nodes, plan_item,
)
from ffl_core.utils import commit
from ffl_integrations.data_service import (
	CATALOG, CATEGORY_TREE, LOCAL, OFF, UNKNOWN, capability, client, require,
)
from ffl_integrations.data_service.client import DataServiceUnavailable

SETTINGS = "Data Service Settings"
TREE_PATH = "/v1/catalog/categories"
MODE_OFF, MODE_REPORT, MODE_APPLY = "Off", "Report only", "Apply"
FEED_LIMIT = 5000

# First contact only (SPEC M3b "existing group by agreed map"): the Item Group a
# tree node already is in this POS. After the first link the Catalog Category row
# holds it; a node whose name is a group's name links by name. Never consulted
# again once linked, so a rename in the workbench does not re-link anything.
AGREED_GROUPS = {
	"firearms": "Firearm",
	"firearms-pistols": "Pistol",
	"firearms-revolvers": "Revolver",
	"firearms-rifles": "Rifle",
	"firearms-shotguns": "Shotgun",
	"firearms-other": "Other Firearm",
	# Owner 2026-09-26: the existing Receiver group (under Firearm) and root group
	# Antique — never a new firearm group. A site without them gets the node
	# reported, not guessed.
	"firearms-receivers-frames": "Receiver",
	"firearms-muzzleloaders-antiques": "Antique",
	"firearms-suppressors": "Suppressor",
	"parts-uppers": "Uppers",
	"optics": "Optic",
	"optics-mounts-rings": "Optic Mounts",
	"magazines": "Magazine",
	"accessories": "Accessory",
	"accessories-weapon-lights": "Light",
	"accessories-iron-sights": "Sights",
	"accessories-knives-tools": "Knife",
	"accessories-holsters-pouches": "Holster",
	"accessories-cleaning": "Cleaning",
	"accessories-safes-security": "Safe",
	"accessories-other": "Misc Accessory",
	"ammunition-handgun": "Pistol Ammo",
	"ammunition-rifle": "Rifle Ammo",
	"ammunition-shotgun": "Shotgun Ammo",
	"ammunition-rimfire": "Rimfire Ammo",
	"services": "Service",
}

# ---- settings -----------------------------------------------------------------

def mode() -> str:
	"""The mirror's mode; a site that never saved the setting runs Report only."""
	return frappe.db.get_single_value(SETTINGS, "category_mirror") or MODE_REPORT


def _setting(field):
	return frappe.db.get_single_value(SETTINGS, field) or ""


# ---- the tree -----------------------------------------------------------------

def fetch_tree(since: str | None = None) -> tuple[list, str]:
	"""Every node changed after ``since`` (all of them without one) and the new
	cursor. Pages until the service says there is no more."""
	from ffl_integrations.distributor import remote_catalog

	backend = remote_catalog._backend()
	out, cursor = [], (since or "")
	while True:
		params = {"since": cursor, "limit": FEED_LIMIT} if cursor else None
		data = (backend.categories(cursor or None) if backend is not None
			else client.get(TREE_PATH, params)) or {}
		out += data.get("nodes") or []
		cursor = str(data.get("cursor") if data.get("cursor") is not None else cursor)
		if not data.get("more"):
			return out, cursor


def upsert_nodes(nodes) -> int:
	"""Write the served nodes into Catalog Category (parents first, as served); the
	paths are recomputed from the parent links afterwards (a rename emits only the
	renamed node). Returns how many rows changed."""
	changed = 0
	for n in nodes:
		values = {"node_name": n.get("name") or n["slug"], "parent_slug": n.get("parent") or None,
			"level": cint(n.get("level")), "position": cint(n.get("position")),
			"active": 1 if n.get("active", True) else 0, "menu": 1 if n.get("menu") else 0,
			"store_hint": n.get("store_hint") or None, "seq": cint(n.get("seq"))}
		if frappe.db.exists(DOCTYPE, n["slug"]):
			cur = frappe.db.get_value(DOCTYPE, n["slug"], list(values), as_dict=True)
			if any((cur.get(k) or None) != (v or None) for k, v in values.items()):
				frappe.db.set_value(DOCTYPE, n["slug"], values, update_modified=False)
				changed += 1
		else:
			frappe.get_doc({"doctype": DOCTYPE, "name": n["slug"], "slug": n["slug"], **values}).db_insert()
			changed += 1
	_recompute_paths()
	return changed


def _recompute_paths() -> None:
	rows = {r.slug: r for r in frappe.get_all(DOCTYPE, fields=["slug", "node_name", "parent_slug", "path"])}
	for slug, r in rows.items():
		names, walk, seen = [], slug, set()
		while walk and walk in rows and walk not in seen:
			seen.add(walk)
			names.append(rows[walk].node_name)
			walk = rows[walk].parent_slug
		path = " › ".join(reversed(names))
		if path != (r.path or ""):
			frappe.db.set_value(DOCTYPE, slug, "path", path, update_modified=False)


# ---- Item Groups --------------------------------------------------------------

def link_groups(*, apply: bool) -> dict:
	"""Link every node to its Item Group: the one already linked; else the agreed
	group / a group of the node's name (both modes — a link to an existing group
	changes nothing); else, applying, a new group under the parent node's group.
	A node whose parent files as a leaf group files as that leaf (no group of its
	own). A moved node re-parents its group — never a firearm group, which is
	reported instead. Returns ``{actions, issues}``."""
	rows = nodes()
	claimed = {r.item_group: r.slug for r in rows if r.item_group}
	actions, issues = [], []

	def parent_group(r):
		return group_for(r.parent_slug) if r.parent_slug else ROOT_GROUP

	for r in rows:
		if r.item_group and not frappe.db.exists("Item Group", r.item_group):
			issues.append(_issue(r, f"its Item Group {r.item_group} no longer exists — relinked"))
			claimed.pop(r.item_group, None)
			frappe.db.set_value(DOCTYPE, r.slug, {"item_group": None, "group_parent": None},
				update_modified=False)
			r.item_group = None
		if r.item_group:
			if (r.group_parent or "") != (r.parent_slug or ""):
				_move_group(r, parent_group(r), apply=apply, actions=actions, issues=issues)
			continue
		candidate = AGREED_GROUPS.get(r.slug)
		if not (candidate and frappe.db.exists("Item Group", candidate)):
			candidate = r.node_name if frappe.db.exists("Item Group", r.node_name) else None
		if candidate:
			if claimed.get(candidate, r.slug) != r.slug:
				issues.append(_issue(r, f"Item Group {candidate} is already linked to "
					f"{claimed[candidate]} — not linked"))
				continue
			claimed[candidate] = r.slug
			_link_group(r, candidate)
			actions.append(_action(r, "link group", candidate))
			continue
		if not r.active:
			continue
		under = parent_group(r)
		if not under or not _is_group_node(under):
			continue  # files as its parent's leaf group
		if is_firearm_group(under):
			issues.append(_issue(r, f"a firearm category with no agreed Item Group — it files as "
				f"{under} until the owner names its group"))
			continue
		actions.append(_action(r, "create group", f"{r.node_name} under {under}"))
		if apply:
			frappe.get_doc({"doctype": "Item Group", "item_group_name": r.node_name,
				"parent_item_group": under, "is_group": 0}).insert(ignore_permissions=True)
			_link_group(r, r.node_name)
			claimed[r.node_name] = r.slug
	return {"actions": actions, "issues": issues}


def _link_group(r, group) -> None:
	frappe.db.set_value(DOCTYPE, r.slug, {"item_group": group, "group_parent": r.parent_slug or ""},
		update_modified=False)
	r.item_group, r.group_parent = group, r.parent_slug or ""


def _move_group(r, under, *, apply, actions, issues) -> None:
	"""A node that moved since its group was placed: the group follows it."""
	group = r.item_group
	current = frappe.db.get_value("Item Group", group, "parent_item_group")
	if is_firearm_group(group) or (under and is_firearm_group(under)):
		issues.append(_issue(r, f"moved in the tree, but {group} is a firearm Item Group — "
			f"not re-parented (owner to confirm)"))
		return
	if not under or not _is_group_node(under) or under == group:
		issues.append(_issue(r, f"moved under a category that files as the leaf group {under} — "
			f"{group} stays under {current}"))
		return
	if under != current:
		actions.append(_action(r, "move group", f"{group}: {current} → {under}"))
		if not apply:
			return
		doc = frappe.get_doc("Item Group", group)
		doc.parent_item_group = under
		doc.save(ignore_permissions=True)
	if apply:
		frappe.db.set_value(DOCTYPE, r.slug, "group_parent", r.parent_slug or "", update_modified=False)


def _action(r, what, detail) -> dict:
	return {"category": r.slug, "path": r.path or r.node_name, "action": what, "detail": detail}


def _issue(r, detail) -> dict:
	return {"category": r.slug, "path": r.path or r.node_name, "issue": detail}


# ---- products -------------------------------------------------------------------

def master_product(key):
	"""The master's product for a key (UPC, else ``<house>:<sku>``), or None — also
	when the service cannot be reached (the caller falls back)."""
	from ffl_integrations.distributor import catalog

	if not key:
		return None
	try:
		return catalog.product(key, in_houses=[])
	except DataServiceUnavailable:
		return None


def classify(row, house, *, title=None) -> frappe._dict:
	"""How to file a catalog row at promotion: ``{item_group, is_firearm, category}``.

	``is_firearm``: the law's answer — the service's class, which the master derives
	from its legal fields (``legal_firearm``). The group: the master category's,
	else the title / class fallback (the master unreachable or silent)."""
	from ffl_integrations.distributor.util import legal_firearm

	from ffl_integrations.distributor.util import master_key

	row = row or {}
	title = title or row.get("short_description") or row.get("description")
	product = master_product(master_key(row, house))
	# One read: the product's legal answer decides legal_firearm (None → fallback).
	is_firearm = legal_firearm(dict(row, legal=(product or {}).get("legal")), house)
	slug = category_of(product)
	group = group_for_product(slug, is_firearm=is_firearm) if slug else None
	return frappe._dict(item_group=group or fallback_group(row, is_firearm=is_firearm, title=title),
		is_firearm=is_firearm, category=slug)


def fallback_group(row, *, is_firearm, title=None) -> str:
	"""The group from the service's class and the title, for when the master has no
	category: distributor-agnostic (no department tables)."""
	from ffl_core.accessory_groups import (
		SUPPRESSOR_TITLE, ammo_group, refine_accessory_group, refine_parts_group,
	)

	klass = str((row or {}).get("product_class") or "").strip()
	title = title or ""
	if is_firearm:
		if klass == "NFA":
			group = "Suppressor" if SUPPRESSOR_TITLE.search(title) else "NFA"
		else:
			group = {"Handgun": "Pistol", "Long Gun": "Rifle"}.get(klass, "Other Firearm")
	elif klass == "Ammunition":
		group = ammo_group(title) or "Ammunition"
	elif klass == "Gun Parts":
		group = refine_parts_group(None, title) or refine_accessory_group("Misc Accessory", title)
	else:
		group = refine_accessory_group("Misc Accessory", title)
		if group == "Misc Accessory":
			group = refine_parts_group(None, title) or group
	return group if group and frappe.db.exists("Item Group", group) else ROOT_GROUP


def item_key(item) -> str | None:
	"""The master's key for a local Item: its UPC field, a UPC-shaped item code or
	barcode, else ``<house>:<item code>`` for a drop-ship orphan."""
	upc = (item.get("upc") or "").strip()
	if upc:
		return upc
	code = item.get("name") or item.get("item_code") or ""
	if code.isdigit() and 8 <= len(code) <= 14:
		return code
	# The inverse of rsr.promote.barcode_owner (UPC → Item): the Item's UPC-shaped
	# barcode, by the same UPC-shape rule as the item code above.
	for barcode in frappe.get_all("Item Barcode", filters={"parent": code, "parenttype": "Item"},
			pluck="barcode", order_by="idx asc"):
		if barcode and barcode.isdigit() and 8 <= len(barcode) <= 14:
			return barcode
	dist = item.get("dropship_distributor")
	if dist:
		provider = frappe.db.get_value("Distributor", dist, "provider")
		if provider:
			return f"{provider}:{code}"
	return None


ITEM_FIELDS = ["name", "item_name", "upc", "item_group", "catalog_category", "is_firearm",
	"has_serial_no", "dropship_distributor", "disabled"]


# ---- the stores (the web app answers through hooks: the channel apps never import
# each other — ffl_woo_sync registers both in its hooks.py) --------------------------

TICK_JOB, MIRROR_JOB = "catalog_category_tick", "catalog_category_mirror"
STORE_MIRROR_HOOK = "catalog_category_store_mirror"
RECATEGORIZE_HOOK = "catalog_category_recategorize"


def _store_mirror(*, apply: bool) -> dict:
	"""``{site: {actions, issues, unlinked_terms}}`` from every store app installed."""
	out = {}
	for path in frappe.get_hooks(STORE_MIRROR_HOOK):
		out.update(frappe.get_attr(path)(apply=apply) or {})
	return out


def _recategorize(item_code, ctx) -> None:
	"""Each store points the Item's product at its category (category only)."""
	for path in frappe.get_hooks(RECATEGORIZE_HOOK):
		frappe.get_attr(path)(item_code, ctx)


# ---- the tick -------------------------------------------------------------------

def run_mirror(*, full: bool = False) -> dict:
	"""One mirror pass: read the tree (the feed after the first full read), link
	Item Groups, then each configured store's categories. Writes the report to the
	settings and an Error Log when the set of things it could not link changes."""
	current = mode()
	if current == MODE_OFF:
		return {"mode": current}
	apply = current == MODE_APPLY
	report = {"mode": current, "run_at": str(now_datetime())}
	try:
		since = None if full else (_setting("category_tree_cursor") or None)
		served, cursor = fetch_tree(since)
		report["tree"] = {"read": "full" if not since else "changes", "nodes": len(served),
			"changed": upsert_nodes(served)}
		frappe.db.set_single_value(SETTINGS, "category_tree_cursor", cursor)
		report["groups"] = link_groups(apply=apply)
		if not cint(_setting("category_products_baselined")):
			# Before Apply is ever possible: the follower starts from here.
			report["product_feed_baseline"] = baseline_product_feed()
		commit()
	except DataServiceUnavailable as exc:
		report["error"] = f"catalog service: {exc}"
		_save_report(report)
		return report
	report["stores"] = _store_mirror(apply=apply)
	_save_report(report)
	return report


def _save_report(report) -> None:
	issues = list((report.get("groups") or {}).get("issues") or [])
	for store in (report.get("stores") or {}).values():
		issues += store.get("issues") or []
	if report.get("error"):
		issues.append({"issue": report["error"]})
	previous = _setting("category_mirror_report")
	frappe.db.set_single_value(SETTINGS, {"category_mirror_report": json.dumps(report, indent=1, default=str),
		"category_mirror_issues": len(issues), "category_mirror_run_at": now_datetime()})
	commit()
	if issues and _digest(issues) != _digest(_previous_issues(previous)):
		frappe.log_error(title=f"Category mirror: {len(issues)} categories could not be linked",
			message=json.dumps(issues, indent=1))


def _previous_issues(text) -> list:
	try:
		prev = json.loads(text) if text else {}
	except ValueError:
		return []
	out = list((prev.get("groups") or {}).get("issues") or [])
	for store in (prev.get("stores") or {}).values():
		out += store.get("issues") or []
	if prev.get("error"):
		out.append({"issue": prev["error"]})
	return out


def _digest(issues) -> str:
	return hashlib.sha1(json.dumps(issues, sort_keys=True, default=str).encode()).hexdigest()


def _halves() -> tuple[bool, bool]:
	"""(mirror the tree, follow the product changes) for this tick: each needs its
	own capability, and the mode (Off: neither; following: Apply only). A capability
	whose settings could not be read (``unknown``) skips the whole tick."""
	current = mode()
	if current == MODE_OFF:
		return False, False
	# The product changes are a cross-house read (/products/changes): only Data
	# Service Settings itself (or Local) serves it, not a house's own service.
	tree, products = capability(CATEGORY_TREE), capability(CATALOG, house=None)
	if UNKNOWN in (tree, products):
		return False, False
	return tree != OFF, current == MODE_APPLY and products != OFF


def clear_stale_mirror_state() -> bool:
	"""The tree is off (no Data Service): the last mirror report and its issue count
	describe a source this site no longer has, so they are cleared — once: a clean
	state is left alone, so later ticks write nothing. True when it cleared."""
	if not cint(_setting("category_mirror_issues")) and not _setting("category_mirror_report"):
		return False
	frappe.db.set_single_value(SETTINGS, {"category_mirror_issues": 0, "category_mirror_report": ""})
	commit()
	return True


def scheduled() -> None:
	"""Cron (every 15 min): the tick runs on the long queue — a store's category
	list and the product feed can outlast the default queue's timeout. Nothing to
	do (mode Off, or neither half set up) enqueues nothing."""
	if capability(CATEGORY_TREE) == OFF:   # confirmed off: never on an unread setting
		clear_stale_mirror_state()
	if not any(_halves()) or catalog_tree.legacy_job_queued(TICK_JOB):
		return
	frappe.enqueue("ffl_integrations.catalog_master.tick", queue="long", timeout=1800,
		job_id=TICK_JOB, deduplicate=True, now=frappe.flags.in_test)


def tick() -> None:
	"""The mirror (``category_tree``), then (Apply, ``catalog``) the master's product
	category changes."""
	mirror, follow = _halves()
	if mirror:
		run_mirror()
	if follow:
		try:
			follow_product_changes()
		except DataServiceUnavailable as exc:
			frappe.logger("catalog_master").warning(f"product changes not read: {exc}")


@frappe.whitelist()
def run_category_mirror(full: int = 0) -> dict:
	"""Settings button: run the category mirror now, in its current mode.

	Args:
	    full: 1 re-reads the whole tree instead of the changes since the last run.

	Returns:
	    {queued: true} — the run is a background job; its report lands on the
	    settings' Categories tab.

	Permissions:
	    System Manager.
	"""
	frappe.only_for("System Manager")
	require(CATEGORY_TREE)
	if not catalog_tree.legacy_job_queued(MIRROR_JOB):
		frappe.enqueue("ffl_integrations.catalog_master.run_mirror", queue="long", timeout=1800,
			job_id=MIRROR_JOB, deduplicate=True, full=bool(cint(full)), now=frappe.flags.in_test)
	return {"queued": True}


# ---- the master's product changes and the migration ------------------------------

def _items_for_keys(keys) -> dict:
	"""``{product key: [Item rows]}`` — the local Items a master key names (UPC field,
	item code, barcode; ``<house>:<sku>`` names the drop-ship orphan item code)."""
	keys = [k for k in dict.fromkeys(keys or []) if k]
	if not keys:
		return {}
	codes = {k.split(":", 1)[1]: k for k in keys if ":" in k}
	plain = [k for k in keys if ":" not in k]
	names = set()
	if plain:
		names |= {r[0] for r in frappe.db.sql(
			"SELECT name FROM `tabItem` WHERE upc IN %(k)s OR name IN %(k)s", {"k": plain})}
		names |= {r[0] for r in frappe.db.sql(
			"SELECT parent FROM `tabItem Barcode` WHERE parenttype='Item' AND barcode IN %(k)s",
			{"k": plain})}
	if codes:
		names |= {r[0] for r in frappe.db.sql("SELECT name FROM `tabItem` WHERE name IN %(k)s",
			{"k": list(codes)})}
	if not names:
		return {}
	out: dict = {}
	wanted = set(keys)
	for item in frappe.get_all("Item", filters={"name": ("in", list(names)), "disabled": 0},
			fields=ITEM_FIELDS):
		key = item_key(item)
		if key in wanted or codes.get(item.name) in wanted:
			out.setdefault(key if key in wanted else codes[item.name], []).append(item)
	return out


def _plan(pairs, *, marks=None) -> list:
	"""``[(item row, product)]`` → the planned changes (``catalog_tree.plan_item``).
	``marks`` (the local catalog, ``FeedLegalMarks``): a product with no master
	category is still checked against its offers' firearm answer
	(``_feed_legal_change``)."""
	out = []
	for item, product in pairs:
		change = plan_item(item, product)
		if change is None and marks is not None:
			change = _feed_legal_change(item, product, marks)
		if change:
			out.append(change)
	return out


class FeedLegalMarks:
	"""The local follower's reported firearm differences, ``{item: digest}``, in ONE
	cache value: read once per tick, written at most once per page, and only after
	that page's Error Log rows and commit went through (``flush``) — a failure
	before then leaves the difference unreported, so it is reported again.
	# lazy: the digests live in the cache (a flush re-reports each standing
	# difference once); a column on the Item would make them permanent."""

	KEY = "catalog_master:feed_legal"

	def __init__(self):
		self.stored = frappe.cache().get_value(self.KEY) or {}
		self.pending: dict = {}   # item -> digest to record, or None to forget

	def has(self, item_name) -> bool:
		return item_name in self.stored

	def reported(self, item_name, digest) -> bool:
		return self.pending.get(item_name, self.stored.get(item_name)) == digest

	def mark(self, item_name, digest) -> None:
		self.pending[item_name] = digest

	def forget(self, item_name) -> None:
		if item_name in self.stored or self.pending.get(item_name):
			self.pending[item_name] = None

	def flush(self) -> None:
		"""Record this page's marks (call after its commit). Nothing changed: no write."""
		if not self.pending:
			return
		for name, digest in self.pending.items():
			if digest is None:
				self.stored.pop(name, None)
			else:
				self.stored[name] = digest
		self.pending = {}
		frappe.cache().set_value(self.KEY, self.stored)


def _feed_checked(item, marks) -> bool:
	"""Is this Item's firearm flag checked against the local feed? Not serialized,
	and not filed by the catalog master (``catalog_category``: the master's answer
	stands) unless a difference is on record for it (so an agreement can clear it)."""
	return not cint(item.get("has_serial_no")) and (
		not item.get("catalog_category") or marks.has(item.get("name")))


def _feed_legal_change(item, product, marks) -> dict | None:
	"""An uncategorised product's firearm answer from its offers (``legal_firearm``:
	the house's FFL flag, then the class — what every Local consumer uses) against
	the Item's ``is_firearm``: a difference is reported for the owner, never applied
	(``plan_item``'s legal rule). Serialized Items are never touched.

	Reported once per conclusion: the local feed names a product again on every
	price or quantity change, so the item's last reported issue is kept in ``marks``
	(by digest, as the mirror keeps its issue set) and only a different one is
	reported; an agreement clears it, so a later difference is new again. The mark
	is recorded by ``marks.flush`` once the report is committed."""
	from ffl_integrations.distributor.util import legal_firearm

	offers = (product or {}).get("offers") or []
	if not offers or not _feed_checked(item, marks) or category_of(product):
		return None
	issue = legal_issue(item, {"is_firearm": any(legal_firearm(o, o.get("house")) for o in offers)})
	if not issue:
		marks.forget(item["name"])
		return None
	digest = _digest([item["name"], issue])
	if marks.reported(item["name"], digest):
		return None
	marks.mark(item["name"], digest)
	return {"item": item["name"], "item_name": item.get("item_name"),
		"old_group": item.get("item_group"), "old_category": item.get("catalog_category"),
		"new_category": item.get("catalog_category"), "new_group": item.get("item_group"),
		"issues": [issue]}


def feed_head() -> str:
	"""The ``/products/changes`` cursor as of now (every house at its latest).
	# lazy: pages through the feed once (5000 keys a house a page) to reach its head;
	# a `since=latest` on the service would make it one call."""
	from ffl_integrations.distributor import catalog

	cursor = ""
	while True:
		page = catalog.changes(cursor)
		cursor = page.get("cursor") or cursor
		if not page.get("more"):
			return cursor


def baseline_product_feed() -> str:
	"""Point the product-changes cursor at the feed's head: the follower then takes
	only changes made from now on. Items already on file move only through
	``m3_resync`` (dry run → owner → apply), never by the follower."""
	head = feed_head()
	frappe.db.set_single_value(SETTINGS, {"category_products_cursor": head,
		"category_products_baselined": 1})
	return head


def follow_product_changes() -> dict:
	"""Cron (Apply): the products whose master category or legal fields changed
	after the baseline (``/products/changes``) → their Items re-filed and their store
	products re-categorised, one commit per Item. Never walks the feed from its
	start: without a baseline it takes one and moves nothing."""
	from ffl_integrations.distributor import catalog

	cursor = frappe.db.get_single_value(SETTINGS, "category_products_cursor") or ""
	if not cint(frappe.db.get_single_value(SETTINGS, "category_products_baselined")):
		baseline_product_feed()
		commit()
		return {"changed": 0, "failed": 0, "issues": 0, "baselined": True}
	ctx: dict = {}
	out = {"changed": 0, "failed": 0, "issues": 0}
	# The local catalog has no master (no category, no legal): the feed's own
	# firearm answer stands in for the legal one, so a flag change is reported
	# exactly as a master legal change is (its offers are read for it, one batched
	# read a page, for the Items that are checked at all).
	marks = FeedLegalMarks() if capability(CATALOG, house=None) == LOCAL else None
	while True:
		page = catalog.changes(cursor)
		keys = page.get("keys") or []
		_forget_legal(keys)
		by_key = _items_for_keys(keys)
		if marks is not None:
			by_key = {k: items for k, items in by_key.items()
				if any(_feed_checked(i, marks) for i in items)}
		if by_key:
			products = catalog.batch(list(by_key), in_houses=None if marks is not None else [])
			pairs = [(item, products.get(k)) for k, items in by_key.items() for item in items]
			for change in _plan(pairs, marks=marks):
				if change["issues"]:
					out["issues"] += 1
					frappe.log_error(title=f"Catalog category: {change['item']} needs review"[:140],
						message="\n".join(change["issues"]))
				if "catalog_category" not in change and "item_group" not in change:
					continue
				out["changed" if _apply_one(change, ctx) else "failed"] += 1
		cursor = page.get("cursor") or cursor
		frappe.db.set_single_value(SETTINGS, "category_products_cursor", cursor)
		commit()
		if marks is not None:
			marks.flush()   # this page's reports are logged and committed
		if not page.get("more"):
			return out


def _forget_legal(keys) -> None:
	"""A changed product's cached legal answer (``util.prefetch_legal``) is dropped:
	one UNLINK a page, not one call a key."""
	if keys:
		frappe.cache().delete_value([f"catalog_master_legal:{key}" for key in keys])


def _apply_one(change, ctx) -> bool:
	frappe.db.savepoint("catalog_category_item")
	try:
		apply_item(change)
		_recategorize(change["item"], ctx)
		commit()
		return True
	except Exception:  # noqa: BLE001 — one Item failing never stops the rest
		frappe.db.rollback(save_point="catalog_category_item")
		frappe.log_error(title=f"Catalog category move failed: {change['item']}"[:140],
			message=frappe.get_traceback())
		return False


# ---- migration ----------------------------------------------------------------------

@frappe.whitelist()
def m3_resync(dry_run: int = 1, token: str | None = None) -> dict:
	"""Move the Items already on file onto the catalog master's categories (SPEC M3b).

	Every enabled catalog Item that is not serialized is filed under its master
	category (``Catalog Category``) and, where the tree names a group it may use,
	that category's Item Group; its store products are re-categorised (category
	only). Serialized Items, firearms and firearm groups are never moved; a legal
	difference is listed, never applied. ``dry_run`` (default) changes nothing and
	also lists what the category mirror would do on each store.

	Args:
	    dry_run: 1 (default) reports only; 0 applies the reported moves in a
	        background job (the category mirror must be in Apply).
	    token: Applying only — the dry run's ``token``. The plan is recomputed and
	        must still be exactly the one the dry run showed, else nothing is done.

	Returns:
	    {items: [{item, item_name, old_group, new_group, old_category, new_category,
	    issues}], moves: {"old → new": count}, counts: {checked, catalog, changes,
	    serialized_skipped, not_in_master, review}, stores: {site: {actions, issues,
	    unlinked_terms}}, token}; applying adds queued: true (failures land in the
	    Error Log, the rest go on) and re-baselines the product-changes cursor.

	Permissions:
	    System Manager.
	"""
	from ffl_integrations.distributor import catalog

	frappe.only_for("System Manager")
	require(CATEGORY_TREE)
	dry = cint(dry_run)
	if not dry and mode() != MODE_APPLY:
		frappe.throw("Switch Data Service Settings › Categories › Mode to Apply first: the "
			"store categories must be linked before products move onto them.")
	items = frappe.get_all("Item", filters={"disabled": 0}, fields=ITEM_FIELDS, order_by="name")
	serialized = [i for i in items if cint(i.has_serial_no)]
	keyed = {}
	for item in items:
		if cint(item.has_serial_no):
			continue
		key = item_key(item)
		if key:
			keyed.setdefault(key, []).append(item)
	products = catalog.batch(list(keyed), in_houses=[]) if keyed else {}
	pairs = [(item, products[k]) for k, rows in keyed.items() if k in products for item in rows]
	changes = _plan(pairs)
	moves: dict = {}
	for c in changes:
		label = f"{c['old_group']} → {c['new_group']}"
		if c["old_group"] != c["new_group"]:
			moves[label] = moves.get(label, 0) + 1
	out = {"items": changes, "moves": moves, "counts": {
		"checked": len(items), "catalog": len(pairs),
		"changes": sum(1 for c in changes if "catalog_category" in c or "item_group" in c),
		"serialized_skipped": len(serialized),
		"not_in_master": sum(len(rows) for k, rows in keyed.items() if k not in products),
		"review": sum(1 for c in changes if c["issues"])}}
	todo = [c for c in changes if "catalog_category" in c or "item_group" in c]
	out["token"] = hashlib.sha1(json.dumps([[c["item"], c.get("catalog_category"), c.get("item_group")]
		for c in todo]).encode()).hexdigest()
	if dry:
		out["stores"] = _store_mirror(apply=False)
		return out
	if (token or "") != out["token"]:
		frappe.throw("The resync plan is not the one the dry run showed (pass its token, or "
			"run the dry run again: the catalog or the Items changed since).")
	# From here the follower takes only later changes; everything the master said
	# until now is in this plan.
	baseline_product_feed()
	frappe.enqueue("ffl_integrations.catalog_master.apply_resync", queue="long", timeout=7200,
		now=frappe.flags.in_test, changes=todo)
	return {**out, "queued": True}


def apply_resync(changes) -> dict:
	"""Job: each planned change written and its store products re-categorised, one
	commit per Item; a failure is logged and the rest go on."""
	ctx: dict = {}
	done = failed = 0
	for change in changes:
		if _apply_one(change, ctx):
			done += 1
		else:
			failed += 1
	return {"done": done, "failed": failed}
