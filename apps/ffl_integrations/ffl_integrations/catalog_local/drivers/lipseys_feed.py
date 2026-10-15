"""Lipsey's feed parsers and class rule, pure (no network, no database).

Ported from the Data Service's Lipsey's driver (``catalog_rows`` /
``pricing_rows``) and its reviewed type map. Both feeds are JSON envelopes
``{success, authorized, errors, data}``: Items/CatalogFeed's ``data`` is the
item list, Items/PricingQuantityFeed's ``data`` is ``{nextUpdate, items}``.

The row's FFL flag is the legal answer: Lipsey's sets ``fflRequired`` exactly on
its firearm and NFA items, receivers and barreled actions included, and
``sotRequired`` exactly on NFA items. The department is Lipsey's free-text
``type``, case-folded and whitespace-collapsed.

The class rule is the type map the Data Service seeds for Lipsey's: each type
names a category node, and a firearm row in a type with a firearm node is filed
there instead. ``product_class`` derives the class from that node the way the
Data Service derives its compatibility class (firearm: pistols / revolvers =
Handgun, rifles / shotguns = Long Gun, any other node = Other Firearm; not a
firearm: parts = Gun Parts, ammunition = Ammunition, the rest Accessory), and an
NFA department or a flagged row with an NFA title is NFA. The Data Service also
narrows a node by the title inside its curated category tree; Local mode has no
tree and keeps the type's class.

Muzzleloaders and revolvers: the Data Service files an unflagged row of those two
types as an antique / black-powder gun from a confirmed list. Local mode has no
such list, so every row of those types is FFL-required here (a deliberate,
stricter deviation) and none is marked antique.
"""
from __future__ import annotations

import re
from decimal import Decimal
from urllib.parse import quote

import frappe

from ffl_integrations.catalog_local import store
from ffl_integrations.catalog_local.drivers.rsr_feed import nfa_title
from ffl_integrations.catalog_local.sync import FeedFormatError

# Local mode links the distributor's own image (as for RSR and Sports South); it
# never downloads or mirrors one.
IMAGE_BASE = "https://www.lipseyscloud.com/images/"

REJECT_RATIO = 0.01
REJECT_RATIO_FLOOR = 5_000
ALLOCATED = "Allocated"
NFA_OTHER = "nfa - other"
NFA_OTHER_NAME = "NFA - Other"


def department_key(raw) -> str:
	"""``type`` is free text: case-folded and whitespace-collapsed, so casing drift
	never mints a second department."""
	return re.sub(r"\s+", " ", str(raw or "").strip()).casefold()


# The Data Service's reviewed Lipsey's type map: (type, category node, firearm node).
# The node names keep the Data Service's slugs so the two can be compared line by line.
TYPE_MAP = (
	# firearms
	("Rifle", "firearms-rifles", None),
	("Semi-Auto Pistol", "firearms-pistols", None),
	("Shotgun", "firearms-shotguns", None),
	("Revolver", "firearms-muzzleloaders-antiques", "firearms-revolvers"),
	("Specialty Handgun", "firearms-pistols", None),
	("Combo", "firearms-rifles", None),
	("Other Firearm", "firearms-other", None),
	("Muzzleloader", "firearms-muzzleloaders-antiques", None),
	("NFA - Silencer", "firearms-suppressors", None),
	("NFA - Short Barrel Rifle", "firearms-other-nfa-sbr", None),
	("NFA - Short Barrel Shotgun", "firearms-other-nfa-sbs", None),
	("NFA - Other", "firearms-nfa", None),
	# receivers / frames / barreled actions (Lipsey's flags them FFL)
	("Accessory-Lowers", "parts-lower-parts", "firearms-receivers-frames"),
	("Accessory-Frames", "parts-other", "firearms-receivers-frames"),
	("Accessory-Receiver Sets", "parts-other", "firearms-receivers-frames"),
	("Accessory-Actions", "parts-barrels", "firearms-receivers-frames"),
	("Accessory-Uppers", "parts-uppers", "firearms-receivers-frames"),
	# parts
	("Accessory-Barrels", "parts-barrels", None),
	("Accessory-Chokes/Choke Tools", "parts-barrels-shotgun", None),
	("Accessory-Bolts", "parts-bcg", None),
	("Accessory-Charging Handles", "parts-bcg", None),
	("Accessory-Triggers", "parts-triggers", None),
	("Accessory-Safeties", "parts-lower-parts", None),
	("Accessory-Magazine Releases", "parts-lower-parts", None),
	("Accessory-Handguards", "parts-handguards", None),
	("Accessory-Rails", "parts-handguards", None),
	("Accessory-Forends", "parts-stocks-grips", None),
	("Accessory-Stocks", "parts-stocks-grips", None),
	("Accessory-Grips", "parts-stocks-grips", None),
	("Accessory-Braces", "parts-stocks-grips-stocks-braces", None),
	("Accessory-Muzzle Brakes", "parts-muzzle-devices-brakes-comps", None),
	("Accessory-Flash Hiders", "parts-muzzle-devices-flash-hiders", None),
	("Accessory-Silencer Accessories", "parts-muzzle-devices", None),
	("Accessory-Slides", "parts-slides-handgun", None),
	("Accessory-Slide Assemblies", "parts-slides-handgun", None),
	("Accessory-Parts", "parts-other", None),
	("Accessory-Kits", "parts-other", None),
	# magazines
	("Accessory-Magazines", "magazines", None),
	("Accessory-Magazine Loaders", "magazines-loaders", None),
	# optics
	("Accessory-Scopes", "optics-rifle-scopes", None),
	("Accessory-Lasers and Sights", "optics-red-dots", None),
	("Accessory-Bases and Mounts", "optics-mounts-rings", None),
	("Accessory-Rings & Ring Mounts", "optics-mounts-rings", None),
	("Accessory-Binoculars", "optics-binoculars", None),
	("Accessory-Range Finders", "optics-rangefinders", None),
	("Accessory-Range Finding Bino", "optics-rangefinders", None),
	("Accessory-Monocular", "optics-night-vision-thermal", None),
	("Accessory-Thermal", "optics-night-vision-thermal", None),
	("Accessory-Thermal Binocular", "optics-night-vision-thermal", None),
	("Accessory-Spotting Scopes", "optics-spotting-scopes", None),
	# accessories
	("Accessory-Lights", "accessories-weapon-lights", None),
	("Accessory-Lights and Lasers", "accessories-weapon-lights", None),
	("Accessory-Holsters", "accessories-holsters-pouches", None),
	("Accessory-Magazine Pouches", "accessories-holsters-pouches", None),
	("Accessory-Slings and Swivels", "accessories-slings-mounts", None),
	("Accessory-Bipods", "accessories-bipods-rests", None),
	("Accessory-Cases", "accessories-cases-storage", None),
	("Accessory-Range Bags", "accessories-cases-storage", None),
	("Accessory-Cleaning", "accessories-cleaning", None),
	("Accessory-Batteries", "accessories-batteries", None),
	("Accessory-Knives", "accessories-knives-tools", None),
	("Accessory-Tools", "accessories-knives-tools", None),
	("Accessory-Entrenching Tools", "accessories-knives-tools", None),
	("Accessory-Locks", "accessories-safes-security", None),
	("Accessory-Rifles", "accessories-other", None),
	("Accessory-Assortment", "accessories-other", None),
	("Accessory-CO2 Cartridges", "accessories-non-lethal-airguns", None),
	("Non-Lethal", "accessories-non-lethal-airguns", None),
	("Accessory-Ammunition", "ammunition", None),
)
DEPARTMENTS = {department_key(t): (node, firearm_node) for t, node, firearm_node in TYPE_MAP}

# The NFA types (the Data Service's nfa_departments). A sotRequired row in any
# other type, a new, renamed or blank NFA type included, is re-keyed to
# NFA_OTHER, so it stays NFA.
NFA_KEYS = frozenset(department_key(t) for t in (
	"NFA - Silencer", "NFA - Short Barrel Rifle", "NFA - Short Barrel Shotgun", "NFA - Other"))
# The Data Service's antique / black-powder types. FFL-required here (see the module docstring).
ALWAYS_FFL_KEYS = frozenset(department_key(t) for t in ("Muzzleloader", "Revolver"))

_HANDGUN_NODES = ("firearms-pistols", "firearms-revolvers")
_LONG_GUN_NODES = ("firearms-rifles", "firearms-shotguns")


def _under(node: str, roots) -> bool:
	return any(node == r or node.startswith(r + "-") for r in roots)


def product_class(department, title, ffl_required) -> str:
	"""The class for one Lipsey's row: NFA first (a flagged row in an NFA type or with
	an NFA title), then the type's node, the firearm node for a flagged row."""
	dept = department_key(department)
	ffl = bool(ffl_required)
	if ffl and (dept in NFA_KEYS or nfa_title(title or "")):
		return "NFA"
	node, firearm_node = DEPARTMENTS.get(dept, (None, None))
	if ffl:
		node = firearm_node or node or ""
		if _under(node, _HANDGUN_NODES):
			return "Handgun"
		if _under(node, _LONG_GUN_NODES):
			return "Long Gun"
		return "Other Firearm"
	root = (node or "").split("-", 1)[0]
	return {"parts": "Gun Parts", "ammunition": "Ammunition"}.get(root, "Accessory")


def image_urls(image_name: str) -> tuple[str, str]:
	"""(image_url, image_url_hires): Lipsey's own image URL, the name quoted whole
	(a slash or a space in it never becomes a path or a broken URL)."""
	if not image_name:
		return "", ""
	url = IMAGE_BASE + quote(image_name, safe="")
	return url, url


def money(v) -> Decimal:
	"""A positive finite amount, else 0 (a missing, negative or junk price is no price)."""
	try:
		d = Decimal(str(v)) if v is not None else Decimal(0)
	except ArithmeticError:
		return Decimal(0)
	return d if d.is_finite() and d > 0 else Decimal(0)


def quantity(v) -> int:
	return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _text(v) -> str:
	return str(v if v is not None else "").strip()


def iter_catalog(data, stats: dict | None = None):
	"""CatalogFeed ``data`` -> catalog row dicts for ``store.load_catalog``, one at a
	time. A row without a SKU, either legal flag (fflRequired / sotRequired as
	booleans) or a type is rejected: a renamed key must not load every gun as
	non-FFL. More than 1% rejected (past a 5,000-row floor) raises at the end of the
	stream, before the load touches a live row."""
	total = rejected = 0
	for it in data:
		total += 1
		sku = store.norm_sku(it.get("itemNo")) if isinstance(it, dict) else ""
		kind = (it.get("type") or it.get("itemType")) if sku else None
		if (not sku or not isinstance(it.get("fflRequired"), bool)
				or not isinstance(it.get("sotRequired"), bool) or not _text(kind)):
			rejected += 1
			continue
		dept = department_key(kind)
		if it["sotRequired"] and dept not in NFA_KEYS:
			dept = NFA_OTHER  # an NFA row outside the NFA types stays NFA
		ffl = it["fflRequired"] or it["sotRequired"] or dept in ALWAYS_FFL_KEYS
		allocated = bool(it.get("allocated"))
		cost = money(it.get("currentPrice")) or money(it.get("price"))
		map_price = money(it.get("retailMap"))
		title = " ".join(_text(it.get(k)) for k in ("description1", "description2")).strip()
		image_name = _text(it.get("imageName"))
		image_url, image_url_hires = image_urls(image_name)
		upc = it.get("upc")
		yield {
			"sku": sku,
			# a JSON number has lost its leading zeros
			"upc": (str(upc).zfill(12) if isinstance(upc, int) and not isinstance(upc, bool)
				else _text(upc)),
			"manufacturer": _text(it.get("manufacturer")),
			"model": _text(it.get("model")),
			"department": dept,
			"department_name": NFA_OTHER_NAME if dept == NFA_OTHER else _text(kind),
			"cost": cost,
			"map": map_price,
			"msrp": money(it.get("msrp")),
			# 'M' only where a MAP exists: Lipsey's 0 means "no MAP", not a missing one.
			"map_policy": "M" if map_price else "",
			"qty": 0 if allocated else quantity(it.get("quantity")),
			"ffl_required": ffl,
			"dropship_blocked": not it.get("canDropship"),
			"vendor_approval_required": False,
			"image_url": image_url,
			"discontinued": False,
			"status_flag": ALLOCATED if allocated else "",
			"short_description": title,
			"mfg_number": _text(it.get("manufacturerModelNo")),
			"caliber": _text(it.get("caliberGauge"))[:140],
			"image_name": image_name,
			"image_url_hires": image_url_hires,
			"weight_lb": money(it.get("shippingWeight")),
			"prop65": False,
			"ships_ground_only": False,
			"signature_required": False,
			"product_class": product_class(dept, title, ffl),
		}
	if stats is not None:
		stats["total"], stats["rejected"] = total, rejected
	if total >= REJECT_RATIO_FLOOR and rejected > total * REJECT_RATIO:
		raise FeedFormatError(f"Lipsey's catalog: {rejected}/{total} rows rejected "
			f"(more than {REJECT_RATIO:.0%}) - refusing the feed")
	if rejected:
		frappe.logger("catalog_local", allow_site=True).warning(
			"Lipsey's catalog: %d/%d rows rejected (below the gate, counted only)", rejected, total)


def catalog_skus(data) -> set:
	"""Every item number in the feed, rejected rows included, so a refused row is
	never taken for a vanished one."""
	return {s for s in (store.norm_sku(it.get("itemNo")) for it in data if isinstance(it, dict)) if s}


def pricing_rows(data, stats: dict | None = None) -> list[tuple]:
	"""PricingQuantityFeed ``data`` -> ``[(sku, qty, cost, status_flag)]``. Allocation
	rides the same update as the stock it zeroes. A missing cost is None, so the
	stored cost stays."""
	items = data.get("items") if isinstance(data, dict) else None
	if not isinstance(items, list) or not items:
		raise FeedFormatError("Lipsey's pricing: items is empty or not a list")
	out, rejected = [], 0
	for it in items:
		sku = store.norm_sku(it.get("itemNumber")) if isinstance(it, dict) else ""
		if not sku:
			rejected += 1
			continue
		allocated = bool(it.get("allocated"))
		cost = money(it.get("currentPrice")) or money(it.get("price")) or None
		out.append((sku, 0 if allocated else quantity(it.get("quantity")), cost,
			ALLOCATED if allocated else ""))
	if stats is not None:
		stats["rejected"] = rejected
		stats["next_update"] = _text(data.get("nextUpdate"))
	return out
