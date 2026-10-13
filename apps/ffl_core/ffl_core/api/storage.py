"""Storage Locations page backend: the store map, moving things between locations,
what is not in a location yet, and zone management.

A location is a position in the shop (a numbered slot that holds one firearm, or a
display case / rack / safe that holds anything). This module only reads and writes
the tracking layer in ``ffl_core.storage`` -- it never creates a stock or accounting
document, so nothing here can change what the store owns or what it is worth.
"""
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

from ffl_core import storage
from ffl_core.api.manual_order import default_company
from ffl_core.roles import STOCK_ROLES, STORAGE_ROLES

_SAVEPOINT = "ffl_storage_scan_move"


def _company(company: str | None) -> str:
	company = (company or "").strip() or default_company()
	if not company or not frappe.db.exists("Company", company):
		frappe.throw(_("No company is set up yet."))
	return company


def _gun_row(serial: str) -> dict:
	row = frappe.db.sql(
		"SELECT sn.name AS serial, sn.item_code, i.item_name, sn.manufacturer, sn.model_name, "
		"sn.caliber, sn.storage_location FROM `tabSerial No` sn JOIN `tabItem` i ON i.name = sn.item_code "
		"WHERE sn.name = %s", (serial,), as_dict=True)
	return row[0] if row else {}


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

@frappe.whitelist()
def get_options(company: str | None = None) -> dict:
	"""What the Storage Locations page needs to start: the company, its zones and
	whether the user may create or disable them.

	Args:
	    company: Company to show; defaults to the user's company.

	Returns:
	    {company, can_manage, zones: [{name, zone_name, kind, disabled, positions}]}.
	"""
	frappe.only_for(STORAGE_ROLES)
	company = _company(company)
	zones = frappe.get_all("Storage Zone", filters={"company": company},
		fields=["name", "zone_name", "kind", "disabled", "slot_count as positions"],
		order_by="sort_order asc, creation asc")
	return {"company": company, "zones": zones,
		"can_manage": bool(set(frappe.get_roles()) & set(STOCK_ROLES))}


@frappe.whitelist()
def list_locations(company: str | None = None, include_disabled: int = 0) -> list[dict]:
	"""Every location, for pickers and the label printer.

	Args:
	    company: Company to list; defaults to the user's company.
	    include_disabled: 1 to include disabled locations.

	Returns:
	    [{name, label, zone, kind, position, disabled, free}] in zone and position
	    order. ``free`` is true for a Slots position with no firearm in it (always
	    true for an Open position).
	"""
	frappe.only_for(STORAGE_ROLES)
	company = _company(company)
	rows = frappe.get_all("Storage Location", filters={"company": company},
		fields=["name", "label", "zone", "kind", "position", "disabled"],
		order_by="zone asc, position asc")
	# a position in a disabled zone is disabled too
	off_zones = set(frappe.get_all("Storage Zone", filters={"company": company, "disabled": 1}, pluck="name"))
	for r in rows:
		r["disabled"] = 1 if (r["disabled"] or r["zone"] in off_zones) else 0
	if not cint(include_disabled):
		rows = [r for r in rows if not r["disabled"]]
	taken = {r[0] for r in frappe.db.sql(
		"SELECT DISTINCT storage_location FROM `tabSerial No` WHERE status = 'Active' "
		"AND IFNULL(storage_location, '') != ''")}
	for r in rows:
		r["free"] = r["kind"] != storage.KIND_SLOTS or r["name"] not in taken
	return rows


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def location_query(doctype, txt, searchfield, start, page_len, filters):
	"""Link-field search for Storage Location: enabled locations of the user's company,
	best match first, with a Slots position already holding a gun marked as taken.

	Args:
	    doctype: Always Storage Location (set by the Link control).
	    txt: What the user typed.
	    searchfield: Unused; the label and the name (barcode) are searched.
	    start: Offset into the result.
	    page_len: Page size.
	    filters: Optional {company, only_free, kind}.

	Returns:
	    [(name, description)] for the dropdown.
	"""
	frappe.only_for(STORAGE_ROLES)
	filters = frappe.parse_json(filters) if isinstance(filters, str) else (filters or {})
	company = _company(filters.get("company"))
	cond, args = ["l.company = %s", "l.disabled = 0"], [company]
	if txt:
		# The name (the barcode) as well as the label: Frappe validates a picked Link value
		# by searching for its exact name through this same query.
		cond.append("(l.label LIKE %s OR l.name LIKE %s)")
		args += [f"%{txt}%", f"%{txt}%"]
	if filters.get("kind") in (storage.KIND_SLOTS, storage.KIND_OPEN):
		cond.append("l.kind = %s")
		args.append(filters["kind"])
	taken_join = ("JOIN `tabStorage Zone` z ON z.name = l.zone AND z.disabled = 0 "
		"LEFT JOIN `tabSerial No` sn ON sn.storage_location = l.name AND sn.status = 'Active' "
		"AND l.kind = 'Slots'")
	if cint(filters.get("only_free")):
		cond.append("sn.name IS NULL")
	rows = frappe.db.sql(
		f"SELECT l.name, l.label, l.zone, l.kind, sn.name FROM `tabStorage Location` l {taken_join} "
		f"WHERE {' AND '.join(cond)} ORDER BY l.zone, l.position LIMIT %s OFFSET %s",
		(*args, cint(page_len) or 20, cint(start)))
	return [(name, f"{label}  ·  " + (_("taken by {0}").format(gun) if gun else
		_("single firearm") if kind == storage.KIND_SLOTS else _("open")))
		for name, label, _zone, kind, gun in rows]


@frappe.whitelist()
def plan_slots(rows) -> dict:
	"""Which slots each pending receive row will fill, in one call, so a receipt of many
	guns never offers the same slot twice.

	Args:
	    rows: Rows in receipt order (list or JSON): {key, count, start?} fills ``start`` and
	        the next free slots in its zone; {key, count, after?} takes the first free slots
	        after ``after`` (the suggestion for the next row). Open locations take none.

	Returns:
	    {key: [location names]}; an empty list for an Open location or a taken start slot.
	"""
	frappe.only_for(STORAGE_ROLES)
	if isinstance(rows, str):
		rows = frappe.parse_json(rows)
	# lazy: 200 rows per call; a receipt is a handful of lines. Raise it if one ever is not.
	return storage.plan_row_slots(list(rows or [])[:200])


@frappe.whitelist()
def get_map(company: str | None = None) -> dict:
	"""The store map: every zone with each position's contents.

	Args:
	    company: Company to show; defaults to the user's company.

	Returns:
	    {company, zones: [{name, zone_name, kind, disabled, slot_lines, numbering, positions, used,
	    locations: [{name, label, position, disabled, guns: [{serial, item_name}],
	    qty, items}]}], plans: [{name, plan_name, width, height, modified,
	    layout: {shapes: [{type, zone?, text?, x, y, w, h, r, rows?}]}}],
	    unassigned: {guns, items, to_confirm}}. ``used`` counts positions holding a
	    gun (Slots) or anything (Open, which has one). ``plans`` are the floor plans
	    (one per room, in feet; x/y is a shape's centre, r its rotation in degrees);
	    every zone shape names a zone in ``zones``.
	"""
	frappe.only_for(STORAGE_ROLES)
	company = _company(company)
	zones = frappe.get_all("Storage Zone", filters={"company": company},
		fields=["name", "zone_name", "kind", "disabled", "slot_lines", "numbering"],
		order_by="sort_order asc, creation asc")
	locs = frappe.get_all("Storage Location", filters={"company": company},
		fields=["name", "label", "zone", "position", "disabled"], order_by="zone asc, position asc")
	guns: dict[str, list] = {}
	for r in frappe.db.sql(
			"SELECT sn.storage_location AS location, sn.name AS serial, i.item_name FROM `tabSerial No` sn "
			"JOIN `tabItem` i ON i.name = sn.item_code WHERE sn.status = 'Active' "
			"AND sn.storage_location IN (SELECT name FROM `tabStorage Location` WHERE company = %s) "
			"ORDER BY sn.name", (company,), as_dict=True):
		guns.setdefault(r.location, []).append({"serial": r.serial, "item_name": r.item_name})
	qty: dict[str, tuple] = {r[0]: (flt(r[1]), r[2]) for r in frappe.db.sql(
		"SELECT location, SUM(qty), COUNT(*) FROM `tabStorage Location Qty` WHERE company = %s "
		"AND qty > 0 GROUP BY location", (company,))}
	by_zone: dict[str, list] = {}
	off_zones = {z.name for z in zones if z.disabled}
	for l in locs:
		q = qty.get(l.name, (0.0, 0))
		by_zone.setdefault(l.zone, []).append({
			"name": l.name, "label": l.label, "position": l.position,
			"disabled": 1 if (l.disabled or l.zone in off_zones) else 0,
			"guns": guns.get(l.name, []), "qty": q[0], "items": q[1]})
	for z in zones:
		z["locations"] = by_zone.get(z.name, [])
		z["positions"] = len(z["locations"])
		z["used"] = sum(1 for l in z["locations"] if l["guns"] or l["qty"])
	plans = frappe.get_all("Storage Floor Plan", filters={"company": company},
		fields=["name", "plan_name", "width", "height", "layout", "modified"], order_by="creation asc")
	zone_names = {z.name for z in zones}
	for p in plans:  # delete_zone takes a zone off its plan; this only guards the contract
		p["layout"] = {"shapes": [s for s in storage.plan_shapes(p.layout)
			if s.get("type") != "zone" or s.get("zone") in zone_names]}
	return {"company": company, "zones": zones, "plans": plans,
		"unassigned": storage.unassigned_counts(company)}


@frappe.whitelist()
def get_location(location: str) -> dict:
	"""What is in one location.

	Args:
	    location: A Storage Location name (or its barcode, which is the same).

	Returns:
	    {location: {name, label, zone, kind, disabled}, guns: [{serial, item_code,
	    item_name, manufacturer, model_name, caliber}], items: [{item_code,
	    item_name, qty}]}.
	"""
	frappe.only_for(STORAGE_ROLES)
	loc = storage.location_row(location)
	guns = frappe.db.sql(
		"SELECT sn.name AS serial, sn.item_code, i.item_name, sn.manufacturer, sn.model_name, sn.caliber "
		"FROM `tabSerial No` sn JOIN `tabItem` i ON i.name = sn.item_code "
		"WHERE sn.storage_location = %s AND sn.status = 'Active' ORDER BY sn.name", (loc.name,), as_dict=True)
	items = frappe.db.sql(
		"SELECT q.item_code, i.item_name, q.qty FROM `tabStorage Location Qty` q "
		"JOIN `tabItem` i ON i.name = q.item_code WHERE q.location = %s AND q.qty > 0 "
		"ORDER BY i.item_name", (loc.name,), as_dict=True)
	return {"location": loc, "guns": guns, "items": items}


@frappe.whitelist()
def stock_locations(item_codes, company: str | None = None) -> dict:
	"""For the pickers on the counter: where each non-serialized item is, so a sale
	line can say which location it came from.

	Args:
	    item_codes: Item codes (list or JSON list).
	    company: Company; defaults to the user's company.

	Returns:
	    {item_code: {locations: [{location, label, zone, qty}], unassigned, total, sole}}.
	    ``unassigned`` is the stock that is in no location; ``sole`` is the one location
	    it can only have come from (exactly one holds it and none is unassigned), else null.
	"""
	frappe.only_for(STORAGE_ROLES)
	if isinstance(item_codes, str):
		item_codes = frappe.parse_json(item_codes)
	# lazy: 200 items per call (a cart / order); the pickers ask for a handful.
	return storage.item_locations(_company(company), list(item_codes or [])[:200])


@frappe.whitelist()
def get_unassigned(company: str | None = None) -> dict:
	"""Everything not in a location yet, and everything the locations over-claim.

	Args:
	    company: Company; defaults to the user's company.

	Returns:
	    {guns: [{serial, item_code, item_name, ...}], items: [{item_code, item_name,
	    stock, located, unassigned}], to_confirm: [{item_code, item_name, stock,
	    located, over}]}. ``to_confirm`` lists items sold or shipped without saying
	    which location they came from.
	"""
	frappe.only_for(STORAGE_ROLES)
	return storage.unassigned_summary(_company(company))


# --------------------------------------------------------------------------- #
# Moving things
# --------------------------------------------------------------------------- #

def _resolve(code: str) -> dict:
	"""A scanned code: a serial number, or an item (barcode / UPC / item code)."""
	from ffl_core.api.inventory_count import _find_item_by_upc, _find_serial
	serial = _find_serial(code)
	if serial:
		return {"kind": "serial", "serial": serial.name, "item_code": serial.item_code}
	item_code = _find_item_by_upc(code) or (code if frappe.db.exists("Item", code) else None)
	if item_code:
		return {"kind": "item", "item_code": item_code}
	return {}


@frappe.whitelist()
def scan_move(to_location: str, code: str, qty: float = 1, from_location: str | None = None) -> dict:
	"""Put what was scanned into a location: a firearm by serial number, or units of
	a non-serialized item by barcode. Nothing here touches stock or the books.

	A quantity comes from ``from_location`` if given, else from the units that are in
	no location, else from the one other location that holds it. When it could come
	from several, nothing moves and ``options`` lists them so the page can ask.

	Args:
	    to_location: Where it goes (a Storage Location name).
	    code: A serial number, UPC, or item code.
	    qty: Units for a non-serialized item (a scan adds 1).
	    from_location: Where the units are taken from, when they are in a location.

	Returns:
	    {result: serial | item | already | choose | error, message, serial,
	    item_code, item_name, qty, to_location, from_location, move (the log row, for
	    Undo), options: [{location, label, qty}] (for choose)}. error means nothing
	    moved; the message says why and what to do.
	"""
	frappe.only_for(STORAGE_ROLES)
	code = (code or "").strip()
	if not code:
		return {"result": "error", "message": _("Nothing was scanned.")}
	if frappe.db.exists("Storage Location", code):
		return {"result": "error", "message": _(
			"{0} is a location. Scan the location first, then the things that go in it.").format(code)}
	found = _resolve(code)
	if not found:
		return {"result": "error", "message": _(
			"{0} is not a serial number or barcode the system knows. If it is a product, add its "
			"barcode to the item.").format(code)}
	loc = storage.location_row(to_location)
	frappe.db.savepoint(_SAVEPOINT)
	try:
		if found["kind"] == "serial":
			res = storage.assign_serial(found["serial"], loc.name, reason="move")
			name = frappe.db.get_value("Item", found["item_code"], "item_name")
			if not res:
				return {"result": "already", "serial": found["serial"], "item_name": name,
					"to_location": loc.name, "message": _("{0} is already in {1}.").format(
						found["serial"], loc.label)}
			return {"result": "serial", "serial": found["serial"], "item_code": found["item_code"],
				"item_name": name, "qty": 1, "to_location": loc.name, "move": res.move,
				"message": _("{0} → {1}").format(found["serial"], loc.label)}
		return _scan_item(loc, found["item_code"], flt(qty) or 1, from_location)
	except frappe.ValidationError as exc:
		frappe.db.rollback(save_point=_SAVEPOINT)
		msg = str(exc)
		frappe.clear_messages()
		return {"result": "error", "message": msg}


def _scan_item(loc, item_code: str, qty: float, from_location: str | None) -> dict:
	company = loc.company
	name = frappe.db.get_value("Item", item_code, "item_name") or item_code
	source = (from_location or "").strip() or None
	if not source:
		info = storage.item_locations(company, [item_code])[item_code]
		others = [l for l in info["locations"] if l["location"] != loc.name]
		if info["unassigned"] + 1e-9 >= qty:
			source = None
		elif len(others) == 1:
			source = others[0]["location"]
		elif others:
			return {"result": "choose", "item_code": item_code, "item_name": name, "qty": qty,
				"to_location": loc.name, "options": others, "message": _(
					"{0} is in several places. Pick where to take it from.").format(name)}
	res = storage.move(reason="move", item_code=item_code, qty=qty, from_location=source,
		to_location=loc.name, check_unassigned=True)
	if not res:
		return {"result": "already", "item_code": item_code, "item_name": name,
			"to_location": loc.name, "message": _("{0} is already in {1}.").format(name, loc.label)}
	return {"result": "item", "item_code": item_code, "item_name": name, "qty": res.qty,
		"to_location": loc.name, "from_location": source, "move": res.move,
		"message": _("{0} × {1} → {2}").format(f"{res.qty:g}", name, loc.label)}


@frappe.whitelist()
def undo_move(move: str) -> dict:
	"""Take back a move made on the page: the firearm or units go back where they were.

	Only a hand move can be undone, only once, and only while the things are still
	where that move left them.

	Args:
	    move: The Storage Move name returned by scan_move.

	Returns:
	    {ok: True, message} or {ok: False, message} when it can no longer be undone.
	"""
	frappe.only_for(STORAGE_ROLES)
	# The original row is locked first: two Undo clicks (two tabs, a double tap) queue up here, and
	# the second finds the first's "undo of" row instead of reversing the move twice.
	m = frappe.db.get_value("Storage Move", move, [
		"name", "kind", "reason", "serial_no", "item_code", "qty", "from_location", "to_location"],
		as_dict=True, for_update=True)
	if not m:
		return {"ok": False, "message": _("That move was not found.")}
	if m.reason not in ("move", "confirm"):
		return {"ok": False, "message": _("Only a move made on this page can be undone.")}
	note = f"undo of {m.name}"
	if frappe.db.sql("SELECT name FROM `tabStorage Move` WHERE note = %s LIMIT 1 FOR UPDATE", (note,)):
		return {"ok": False, "message": _("That move was already undone.")}
	frappe.db.savepoint(_SAVEPOINT)
	try:
		if m.kind == "serial":
			now = frappe.db.get_value("Serial No", m.serial_no, "storage_location")
			if now != m.to_location:
				return {"ok": False, "message": _("{0} has been moved since, so this can't be undone.")
					.format(m.serial_no)}
			storage.assign_serial(m.serial_no, m.from_location, reason="move", note=note)
		else:
			storage.move(reason="move", item_code=m.item_code, qty=m.qty,
				from_location=m.to_location, to_location=m.from_location, note=note)
	except frappe.ValidationError as exc:
		frappe.db.rollback(save_point=_SAVEPOINT)
		frappe.clear_messages()
		return {"ok": False, "message": str(exc)}
	return {"ok": True, "message": _("Moved back.")}


@frappe.whitelist()
def confirm_taken(item_code: str, location: str, qty: float, company: str | None = None) -> dict:
	"""Resolve a "to confirm" item: say which location sold or shipped units really
	came from. They leave that location; stock and the books are untouched.

	Args:
	    item_code: The non-serialized item.
	    location: The location they were taken from.
	    qty: How many.
	    company: Company; defaults to the user's company.

	Returns:
	    {ok: True, move, over}, where ``over`` is how many are still to confirm.
	"""
	frappe.only_for(STORAGE_ROLES)
	company = _company(company)
	# Same item lock every other writer takes, THEN the fresh numbers: two people confirming
	# the same units queue up, and the second sees what the first already deducted.
	frappe.db.get_value("Item", item_code, "name", for_update=True)
	over = -storage.unassigned_qty(company, item_code, lock=True)
	qty = flt(qty)
	if over <= 0:
		frappe.throw(_("{0} has nothing waiting to be confirmed.").format(item_code))
	if qty <= 0 or qty > over + 1e-9:
		frappe.throw(_("Only {0} of {1} is waiting to be confirmed.").format(flt(over), item_code))
	res = storage.take_qty(item_code, location, qty, reason="confirm")
	return {"ok": True, "move": res.move if res else None, "over": flt(over - qty, 3)}


# --------------------------------------------------------------------------- #
# Zones (Stock Manager and up)
# --------------------------------------------------------------------------- #

@frappe.whitelist()
def create_zone(zone_name: str, kind: str, count: int = 1, company: str | None = None,
		sides: int = 0, numbering: str | None = None) -> dict:
	"""Create a zone and its positions. A Slots zone names them A1, A2, ... (one firearm
	each). An Open zone is ONE location named like the zone (Rack 1: no limit, holds
	firearms and everything else); create one zone per rack, case or safe.

	Args:
	    zone_name: Short name, like A or Display Case.
	    kind: Slots or Open.
	    count: How many slots to create in a Slots zone (ignored for Open, which is one location).
	    company: Company; defaults to the user's company.
	    sides: Slots only: how many sides (lines of slots) it has on the map, 1-50; a two-sided
	        rack is 2. 0 (default) fits them to the shape it is drawn as.
	    numbering: Slots with two sides: "Odd / even" (default: 1, 3, 5 ... down one side,
	        2, 4, 6 ... facing them) or "In order" (1, 2, 3 ... down the first side, then the
	        second). One side or three and more always count in order.

	Returns:
	    {zone, positions}.
	"""
	frappe.only_for(STOCK_ROLES)
	return storage.create_zone(_company(company), zone_name, kind, count, sides=sides, numbering=numbering)


@frappe.whitelist()
def set_zone_layout(zone: str, sides: int = 0, numbering: str | None = None) -> dict:
	"""Change how a Slots zone is drawn on the floor plan. Nothing moves: every slot keeps
	its name and what is in it.

	Args:
	    zone: The Storage Zone name.
	    sides: How many sides (lines of slots) it has on the map, 1-50; a two-sided rack is 2.
	        0 (default) fits them to the shape it is drawn as.
	    numbering: "Odd / even" (default: 1, 3, 5 ... down one side, 2, 4, 6 ... facing them)
	        or "In order"; only two sides are counted odd / even.

	Returns:
	    {zone, sides, numbering}.
	"""
	frappe.only_for(STOCK_ROLES)
	return storage.set_zone_layout(zone, sides, numbering)


@frappe.whitelist()
def add_positions(zone: str, count: int) -> dict:
	"""Add slots to the end of a Slots zone (existing ones are never renumbered). An Open zone
	is one location, so this is refused for it.

	Args:
	    zone: The Storage Zone name.
	    count: How many to add.

	Returns:
	    {zone, positions, added}.
	"""
	frappe.only_for(STOCK_ROLES)
	return storage.add_positions(zone, count)


@frappe.whitelist()
def remove_positions(zone: str, count: int) -> dict:
	"""Delete the last slots of a Slots zone (A30, A29, ...). They must be empty, and the
	zone keeps at least one -- delete the zone to remove them all.

	Args:
	    zone: The Storage Zone name.
	    count: How many to delete from the end.

	Returns:
	    {zone, positions, removed}.
	"""
	frappe.only_for(STOCK_ROLES)
	return storage.remove_positions(zone, count)


@frappe.whitelist()
def delete_zone(zone: str, company: str | None = None) -> dict:
	"""Delete a zone and all its positions, and take it off its floor plan. Everything in
	it must be moved out first.

	Args:
	    zone: The Storage Zone name.
	    company: The zone's company; defaults to the user's company.

	Returns:
	    {zone, removed}.
	"""
	frappe.only_for(STOCK_ROLES)
	return storage.delete_zone(zone, _company(company))


@frappe.whitelist()
def set_disabled(disabled: int, zone: str | None = None, location: str | None = None) -> dict:
	"""Disable (or re-enable) a zone with all its positions, or one position. A
	disabled place takes nothing new. Disabling needs it to be empty.

	Args:
	    disabled: 1 to disable, 0 to enable.
	    zone: The Storage Zone name (give this or location).
	    location: One Storage Location name.

	Returns:
	    {disabled, locations}.
	"""
	frappe.only_for(STOCK_ROLES)
	return storage.set_disabled(zone, location, disabled)


# --------------------------------------------------------------------------- #
# Floor plans (Stock Manager and up)
# --------------------------------------------------------------------------- #

@frappe.whitelist()
def create_floor_plan(plan_name: str, width: float = 60, height: float = 40,
		company: str | None = None) -> dict:
	"""Start an empty floor plan for one room or floor of the store.

	Args:
	    plan_name: Name shown on the plan's tab, like Showroom or Back Room.
	    width: Width of the room in feet.
	    height: Depth of the room in feet.
	    company: Company; defaults to the user's company.

	Returns:
	    {plan, modified}.
	"""
	frappe.only_for(STOCK_ROLES)
	return storage.create_floor_plan(_company(company), plan_name, width, height)


@frappe.whitelist()
def save_floor_plan(plan: str, plan_name: str, width: float, height: float, shapes: list | str,
		modified: str, company: str | None = None) -> dict:
	"""Save a floor plan drawn on the Storage Locations page: its name, size and every
	shape on it. Refused if someone else saved the plan after ``modified``.

	Args:
	    plan: The Storage Floor Plan name.
	    plan_name: Name shown on the plan's tab.
	    width: Width of the room in feet.
	    height: Depth of the room in feet.
	    shapes: List of {type: zone|wall|door|area|label, zone?, text?, x, y, w, h,
	        r, rows?}: centre x/y and size w/h in feet, rotation r in degrees, rows =
	        lines of slots across a Slots zone's short side. A zone may sit on one
	        plan only; text has no < or >.
	    modified: The plan's ``modified`` when the editor loaded it.
	    company: The plan's company; defaults to the user's company.

	Returns:
	    {plan, modified, shapes, dropped}. ``dropped`` lists zones left out because
	    they were deleted meanwhile.
	"""
	frappe.only_for(STOCK_ROLES)
	if isinstance(shapes, str):
		shapes = frappe.parse_json(shapes)
	return storage.save_floor_plan(_company(company), plan, plan_name, width, height, shapes, modified)


@frappe.whitelist()
def delete_floor_plan(plan: str, company: str | None = None) -> dict:
	"""Delete a floor plan. Its zones stay as they are, just on no plan.

	Args:
	    plan: The Storage Floor Plan name.
	    company: The plan's company; defaults to the user's company.

	Returns:
	    {plan}.
	"""
	frappe.only_for(STOCK_ROLES)
	return storage.delete_floor_plan(_company(company), plan)
