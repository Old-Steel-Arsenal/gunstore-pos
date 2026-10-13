"""The catalog master's category tree as the POS holds it — the one
place both channel apps read it from.

``Catalog Category`` rows mirror the Data Service's tree (written by
``ffl_integrations.catalog_master``); each node carries the Item Group it files as
and its product-category term on each store (written by
``ffl_woo_sync.woocommerce.categories``). A catalog Item's node is the master's
category (``Item.catalog_category``); any other Item's is its Item Group's node. This
module only reads the tree and plans an Item's move — it never calls the catalog
service or a store.

Never automatic: re-grouping a serialized Item, moving an Item into or out of a
firearm Item Group, changing ``is_firearm`` on an Item that exists."""
from __future__ import annotations

import frappe
from frappe.utils import cint

DOCTYPE = "Catalog Category"
ROOT_GROUP = "All Item Groups"

# The mirror's job ids were ``osa_category_*`` before the rename. A job queued under the old id
# when the new code deploys is still that job.
# lazy: one release (until no queue can hold an osa_category_* job), then delete with its callers.
LEGACY_JOB_PREFIX, JOB_PREFIX = "osa_category", "catalog_category"


def legacy_job_queued(job_id: str) -> bool:
	"""Is the same job queued or running under its pre-rename id? The enqueue paths treat it as
	a duplicate, like ``deduplicate=True`` does for the new id."""
	if not job_id.startswith(JOB_PREFIX):
		return False
	from frappe.utils.background_jobs import is_job_enqueued

	try:
		return is_job_enqueued(LEGACY_JOB_PREFIX + job_id[len(JOB_PREFIX):])
	except Exception:                     # no queue to ask (redis down): enqueue as before
		return False


_NODE_FIELDS = ["name", "slug", "node_name", "parent_slug", "level", "position", "active",
	"menu", "store_hint", "path", "item_group", "group_parent", "woo_term_retail"]


def nodes() -> list:
	"""Every mirrored node, parents before children (level, then position)."""
	return frappe.get_all(DOCTYPE, fields=_NODE_FIELDS, order_by="level asc, position asc, slug asc")


def node(slug):
	return frappe.db.get_value(DOCTYPE, slug, _NODE_FIELDS, as_dict=True) if slug else None


def parents() -> dict:
	"""``{slug: parent slug}`` for every node, one read."""
	return dict(frappe.get_all(DOCTYPE, fields=["name", "parent_slug"], as_list=True))


def lineage(slug, parent_of=None) -> list:
	"""``[slug, its parent, …, the root]`` over ``parent_of`` (:func:`parents`, read
	when not given); a cycle stops the walk."""
	parent_of = parents() if parent_of is None else parent_of
	out = []
	while slug and slug not in out:
		out.append(slug)
		slug = parent_of.get(slug)
	return out


def is_firearm_group(group) -> bool:
	"""A firearm Item Group, or one under it (FastBound types, auto_set_is_firearm and
	per-serial pricing hang off these — the mirror never moves them)."""
	from ffl_core.firearm import ALL_FIREARM_GROUPS

	seen = set()
	while group and group not in seen:
		if group in ALL_FIREARM_GROUPS:
			return True
		seen.add(group)
		group = frappe.db.get_value("Item Group", group, "parent_item_group")
	return False


def _is_group_node(group) -> bool:
	return bool(cint(frappe.db.get_value("Item Group", group, "is_group")))


def group_for(slug) -> str | None:
	"""The Item Group a node files as: its own, else its nearest parent's."""
	seen = set()
	while slug and slug not in seen:
		seen.add(slug)
		row = frappe.db.get_value(DOCTYPE, slug, ["item_group", "parent_slug"], as_dict=True)
		if not row:
			return None
		if row.item_group and frappe.db.exists("Item Group", row.item_group):
			return row.item_group
		slug = row.parent_slug
	return None


def node_for_group(group) -> str | None:
	"""The node an Item Group files under: the node linked to it, else its nearest
	parent group's. None for a group outside the tree (a legacy leaf at the root)."""
	seen = set()
	while group and group not in seen:
		seen.add(group)
		slug = frappe.db.get_value(DOCTYPE, {"item_group": group}, "name")
		if slug:
			return slug
		group = frappe.db.get_value("Item Group", group, "parent_item_group")
	return None


def category_of(product) -> str | None:
	"""The product's master category, when this POS mirrors that node."""
	slug = ((product or {}).get("category") or {}).get("slug")
	return slug if slug and frappe.db.exists(DOCTYPE, slug) else None


def group_for_product(slug, *, is_firearm) -> str | None:
	"""The Item Group a product in ``slug`` files as, or None when the tree cannot
	say: no group linked yet, a group node (not a leaf), or a group on the other
	side of the firearm line from the product — a non-firearm in a firearm group
	would be stamped one on save; a firearm outside one would escape the
	serial-number rule ``auto_set_is_firearm`` enforces."""
	group = group_for(slug)
	if not group or _is_group_node(group):
		return None
	if bool(is_firearm) != is_firearm_group(group):
		return None
	return group


def plan_item(item, product) -> dict | None:
	"""What following the master changes on one Item: ``{item, catalog_category?,
	item_group?, old_group, old_category, issues}`` — None when nothing. Serialized
	Items are never touched; a firearm (or a firearm group) is never re-grouped; a
	legal difference is reported, never applied."""
	if cint(item.get("has_serial_no")) or not product:
		return None
	slug = category_of(product)
	if not slug:
		return None
	change = {"item": item["name"], "item_name": item.get("item_name"),
		"old_group": item.get("item_group"), "old_category": item.get("catalog_category"),
		"new_category": slug, "issues": []}
	if item.get("catalog_category") != slug:
		change["catalog_category"] = slug
	issue = legal_issue(item, product.get("legal"))
	if issue:
		change["issues"].append(issue)
	if not cint(item.get("is_firearm")) and not is_firearm_group(item.get("item_group")):
		group = group_for_product(slug, is_firearm=False)
		if group and group != item.get("item_group"):
			change["item_group"] = group
	change["new_group"] = change.get("item_group") or item.get("item_group")
	if "catalog_category" in change or "item_group" in change or change["issues"]:
		return change
	return None


def legal_issue(item, legal) -> str | None:
	"""The sentence for the owner when the catalog's legal answer (``{is_firearm}``)
	differs from the Item's ``is_firearm`` — never applied — else None."""
	legal = legal or {}
	if legal and bool(legal.get("is_firearm")) != bool(cint(item.get("is_firearm"))):
		return (f"the master says firearm={bool(legal.get('is_firearm'))}, "
			f"the Item says {bool(cint(item.get('is_firearm')))} — not changed (owner to review)")
	return None


def apply_item(change) -> None:
	"""Write one planned change (``plan_item``) to its Item — group and category only."""
	values = {k: change[k] for k in ("catalog_category", "item_group") if k in change}
	if values:
		frappe.db.set_value("Item", change["item"], values)
