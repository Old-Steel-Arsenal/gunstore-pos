"""Promote a Lipsey's catalog row to a local Item (the counter ensure seam and
the listing pipeline both call ``LipseysProvider.promote_to_item``).

Mirrors ``sports_south/promote.py``: identity chain explicit item_code > UPC >
Lipsey's item number, idempotent, MAP over MSRP as the selling default, filed
under the catalog master's category (``catalog_master.classify``). Catalog data
comes from the Data Service, never from Lipsey's own feed. Shares the RSR
promoter's master-data helpers (brand / caliber / barcode / response).
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from ffl_integrations.distributor import remote_catalog
from ffl_integrations.rsr.promote import (
	_ensure_barcode, _ensure_brand, _ensure_caliber, _extract_caliber, _item_response,
	barcode_owner,
)

DISTRIBUTOR_CODE = "lipseys"

# Item.firearm_type per Lipsey's department (the Data Service keys it as the
# feed's `type`, case-folded). Anything else that is a firearm under the law
# (muzzleloaders, combos, NFA rifles / shotguns) reads "Other Firearm", which the
# operator can refine on the Item.
FIREARM_TYPE_BY_DEPARTMENT = {
	"semi-auto pistol": "Pistol",
	"specialty handgun": "Pistol",
	"revolver": "Revolver",
	"rifle": "Rifle",
	"shotgun": "Shotgun",
	"accessory-lowers": "Receiver",
	"accessory-frames": "Receiver",
	"accessory-receiver sets": "Receiver",
	"accessory-actions": "Receiver",
	"nfa - silencer": "Silencer",
}


def firearm_type(department) -> str:
	return FIREARM_TYPE_BY_DEPARTMENT.get(str(department or "").strip().casefold(), "Other Firearm")


def promote_to_item(sku: str, opts: dict | None = None) -> dict:
	opts = dict(opts or {})
	sku = (sku or "").strip()
	if not sku:
		frappe.throw(_("Lipsey's item number is required."))
	r = remote_catalog.cached_row(DISTRIBUTOR_CODE, sku)
	if not r:
		frappe.throw(_("Lipsey's item {0} not found in the catalog.").format(sku))

	upc = (r.get("upc") or "").strip()
	item_code = (opts.get("item_code") or upc or sku).strip()
	if not opts.get("item_code") and not frappe.db.exists("Item", item_code):
		item_code = barcode_owner(upc) or item_code
	if frappe.db.exists("Item", item_code):
		_ensure_barcode(item_code, upc)
		return _item_response(item_code)

	from ffl_integrations.catalog_master import classify
	filed = classify(r, DISTRIBUTOR_CODE)
	is_firearm = filed.is_firearm
	item_group = (opts.get("item_group") or filed.item_group or "").strip()
	# Every firearm is serial-tracked, whatever the caller asked for.
	has_serial_no = 1 if is_firearm else cint(opts.get("has_serial_no") or 0)
	selling_default = float(r.get("map") or 0) or float(r.get("msrp") or 0)
	standard_rate = float(opts.get("standard_rate") or selling_default)
	mfg = (r.get("manufacturer") or "").strip()
	brand = _ensure_brand(mfg) if mfg else None
	caliber_name = (r.get("caliber") or "").strip() or (
		_extract_caliber(r.get("long_description") or r.get("short_description"))
		if is_firearm else None)
	caliber = _ensure_caliber(caliber_name)
	name = (opts.get("item_name") or r.get("short_description") or r.get("model") or sku).strip()

	doc = frappe.get_doc({
		"doctype": "Item",
		"item_code": item_code,
		"item_name": name[:140],
		"item_group": item_group,
		"stock_uom": "Nos",
		"is_stock_item": 1,
		"has_serial_no": has_serial_no,
		"standard_rate": standard_rate or None,
		"description": r.get("long_description") or r.get("short_description") or None,
		"brand": brand,
		"image": (r.get("image_url_hires") or r.get("image_url") or None),
		"manufacturer": brand,
		"model_name": (r.get("model") or "").strip() or None,
		"mpn": (r.get("mfg_number") or "").strip() or None,
		"uniform_sell_price": 1 if cint(opts.get("uniform_sell_price") or 0) else 0,
		"caliber": caliber,
		"firearm_type": firearm_type(r.get("department")) if is_firearm else None,
		"is_firearm": 1 if is_firearm else 0,
		"catalog_category": filed.category,
		"is_used": 0,
		"dealer_price": float(r.get("cost") or 0) or None,
		"map_price": float(r.get("map") or 0) or None,
		"msrp": float(r.get("msrp") or 0) or None,
		"listing_status": "Draft",
	})
	if has_serial_no and not doc.get("serial_no_series"):
		doc.serial_no_series = f"{item_code}-.####"
	if upc:
		doc.append("barcodes", {"barcode": upc, "barcode_type": "UPC"})
	doc.flags.ignore_permissions = True
	doc.insert()
	return _item_response(item_code)
