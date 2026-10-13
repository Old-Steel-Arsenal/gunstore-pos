"""Item-level document hooks.

`sync_upc_barcode` keeps the single user-facing `upc` Data field in sync with
ERPNext's standard `Item Barcode` child table (one row, barcode_type="UPC"),
so the simple one-field UPC UX doesn't break POS / Receive Goods barcode
scanning, the typeahead search, or RSR-scan flows — all of which resolve items
through `tabItem Barcode`.
"""
from __future__ import annotations

import frappe
from frappe.utils import flt

UPC_TYPE = "UPC"
# GTIN family ERPNext validates by exact length. The single user-facing UPC
# field accepts any of these; the subtype is picked from the code's length.
GTIN_TYPES = {"UPC", "EAN", "EAN-8"}


def _gtin_barcode_type(code: str) -> str:
	"""ERPNext barcode_type matching a numeric GTIN's length.

	UPC-A is 12 digits; a 13-digit code is an EAN-13 and an 8-digit code an
	EAN-8. ERPNext's Item.validate_barcode rejects a 13-digit code stamped
	"UPC" as "not a valid UPC", so the single UPC field must pick the type by
	length. Non-numeric / other-length codes keep "UPC" (the historical
	default — let ERPNext surface its own validation message)."""
	if code.isdigit():
		if len(code) == 13:
			return "EAN"
		if len(code) == 8:
			return "EAN-8"
	return UPC_TYPE


def _default_selling_price_list() -> str | None:
	return (
		frappe.db.get_single_value("Selling Settings", "selling_price_list")
		or frappe.db.get_value("Price List", "Standard Selling")
	)


def sync_standard_selling_price(doc, method: str | None = None) -> None:
	"""Item on_update: keep the POS-facing Item Price honest.

	ERPNext only creates an Item Price from standard_rate at item INSERT —
	setting the rate later silently does nothing and the POS card shows no
	price. Rules here:
	  - no selling Item Price yet → create one (any sales item, rate > 0);
	  - uniform_sell_price=1 → the Item Price IS standard_rate (hard sync, so
	    the card never drifts from what the serial picker actually charges);
	  - otherwise an existing record is NEVER touched (manager-curated price).
	Writes to the Selling Settings default price list (same as ERPNext's own
	add_price); a POS Profile pointing at a different selling list is out of
	scope."""
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_import:
		return
	rate = flt(doc.get("standard_rate"))
	if rate <= 0 or not doc.get("is_sales_item"):
		return
	# Per-gun priced (serialized, non-uniform) items must NOT carry an Item
	# Price — it would pre-rate POS lines and silently bypass the
	# serial-sets-the-price flow. standard_rate is just a reference there.
	if doc.get("has_serial_no") and not doc.get("uniform_sell_price"):
		return
	price_list = _default_selling_price_list()
	if not price_list:
		return
	# Hard-sync target: the price the register should actually charge today —
	# an ACTIVE sale undercuts standard_rate (same overlay the serial picker
	# applies, so card, picker and charge never disagree). Date rollovers
	# can't run this hook, so refresh_sale_item_prices re-syncs daily.
	from ffl_core.firearm import item_current_price
	target = flt(item_current_price(doc.name, row=doc)) or rate
	existing = frappe.db.get_value(
		"Item Price",
		{"item_code": doc.name, "price_list": price_list, "selling": 1},
		["name", "price_list_rate"],
		as_dict=True,
	)
	if existing:
		if doc.get("uniform_sell_price") and flt(existing.price_list_rate) != target:
			frappe.db.set_value("Item Price", existing.name, "price_list_rate", target)
		return
	doc.add_price(price_list)
	if doc.get("uniform_sell_price") and target != rate:
		# add_price seeds from standard_rate; a sale already running at create
		# time must land on the card immediately, not at the next daily sweep.
		created = frappe.db.get_value(
			"Item Price",
			{"item_code": doc.name, "price_list": price_list, "selling": 1},
			"name",
		)
		if created:
			frappe.db.set_value("Item Price", created, "price_list_rate", target)


def refresh_sale_item_prices() -> None:
	"""Daily: re-run the uniform-item hard-sync for every Item carrying sale
	fields. A sale window opening or closing is a pure calendar event — no
	Item save fires — so without this sweep the POS card would keep charging
	yesterday's price until someone happened to edit the Item. Items whose
	sale_price was cleared are already handled at save time; ones with a
	price (active, future or expired) all resolve through the same overlay,
	so running them daily is idempotent."""
	for r in frappe.get_all(
		"Item",
		filters={"uniform_sell_price": 1, "sale_price": [">", 0]},
		fields=["name"],
	):
		try:
			sync_standard_selling_price(frappe.get_doc("Item", r.name))
		except Exception:
			frappe.log_error(
				title=f"refresh_sale_item_prices failed: {r.name}",
				message=frappe.get_traceback(),
			)


def sync_upc_barcode(doc, method: str | None = None) -> None:
	"""Reconcile Item.upc <-> a single GTIN row in Item.barcodes.

	Runs on `before_validate`, ahead of ERPNext's own validate_barcode, so the
	barcode subtype is correct by the time core validates it.

	- normalize → any GTIN-family row (UPC/EAN/EAN-8) gets its subtype set from
	              the code's length, so a 13-digit code entered as "UPC" (via the
	              RSR quick-entry helper or the Barcodes grid) becomes an EAN-13
	              and is no longer rejected as an invalid UPC.
	- upc set   → ensure exactly one row equals it, typed by length (replace any
	              stale field-owned row; leave independent EAN/other barcodes
	              that don't match the field value alone).
	- upc empty → if a GTIN row exists (e.g. written by RSR scan / FB import /
	              Receive Goods before the field was shown), back-fill upc from
	              it rather than deleting the row.
	"""
	rows = doc.get("barcodes") or []

	# Normalize the subtype of every GTIN-family row to match its length before
	# core validate_barcode runs (UPC-A=12, EAN-13=13, EAN-8=8). Leave non-GTIN
	# types (CODE-39, GS1-128, …) untouched.
	for r in rows:
		code = (r.barcode or "").strip()
		if code and (r.barcode_type or "").upper() in GTIN_TYPES:
			r.barcode_type = _gtin_barcode_type(code)

	upc = (doc.get("upc") or "").strip()

	if upc:
		doc.upc = upc
		want_type = _gtin_barcode_type(upc)
		before = doc.get_doc_before_save()
		prev = ((before.get("upc") if before else "") or "").strip()
		same = [r for r in rows if (r.barcode or "").strip() == upc]
		# A row the single-field UX owns: the current value, the previous value
		# (a changed UPC/EAN), or any legacy UPC-typed row. Independent EAN/other
		# barcodes that don't match the field value are never field-owned.
		def _owned(r) -> bool:
			b = (r.barcode or "").strip()
			return (
				b == upc
				or (bool(prev) and b == prev)
				or (r.barcode_type or "").upper() == UPC_TYPE
			)

		others_owned = [r for r in rows if r not in same and _owned(r)]
		if len(same) == 1 and (same[0].barcode_type or "").upper() == want_type and not others_owned:
			return
		doc.barcodes = [r for r in rows if not _owned(r)]
		doc.append("barcodes", {"barcode": upc, "barcode_type": want_type})
		return

	# upc empty → reverse-fill from an existing GTIN row (display only).
	for r in rows:
		if (r.barcode_type or "").upper() in GTIN_TYPES:
			val = (r.barcode or "").strip()
			if val:
				doc.upc = val
				return


def sync_item_image_gallery(doc, method: str | None = None) -> None:
	"""Reconcile Item.image (the standard top-right / featured image widget)
	with the `image_gallery` child table so they are a single set of photos.

	Model — the gallery is the full set; exactly one row is primary and
	Item.image always mirrors that primary row:

	  - image set but not yet in the gallery → append it as a (non-primary)
	    row, so a photo uploaded via the top-right widget shows up below.
	  - exactly one primary is guaranteed: a row the operator just ticked wins;
	    otherwise an existing sole primary is kept; otherwise the first row is
	    promoted.
	  - Item.image is then mirrored from the surviving primary row.

	Uploading via the top-right widget therefore adds a photo but never steals
	the featured slot — the featured image is changed only by ticking
	`is_primary` on a gallery row (product decision).
	"""
	img = (doc.get("image") or "").strip()
	gallery = doc.get("image_gallery") or []

	# Absorb a top-right image that isn't represented in the gallery yet.
	if img and not any((r.image or "").strip() == img for r in gallery):
		doc.append("image_gallery", {"image": img, "is_primary": 0})
		gallery = doc.image_gallery

	if not gallery:
		return  # nothing to reconcile — leave Item.image as-is (empty)

	# Pick the surviving primary. A row toggled 0 -> 1 on *this* save wins
	# (so ticking is_primary in the grid changes the featured image); else an
	# established sole primary is kept; else the first row is promoted.
	winner = None
	before = doc.get_doc_before_save()
	if before is not None:
		was_primary = {
			r.name: (1 if r.is_primary else 0)
			for r in (getattr(before, "image_gallery", None) or [])
		}
		newly = [r for r in gallery if r.is_primary and not was_primary.get(r.name)]
		if newly:
			winner = newly[-1]

	if winner is None:
		current = [r for r in gallery if r.is_primary]
		winner = current[0] if current else gallery[0]

	for r in gallery:
		r.is_primary = 1 if r is winner else 0

	# Mirror the featured photo back onto the top-right / featured widget.
	doc.image = winner.image
