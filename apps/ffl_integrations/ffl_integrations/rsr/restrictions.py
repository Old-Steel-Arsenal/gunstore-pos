"""RSR ship-restriction lookup — the fifth RSR feed.

``keydealer/rsr-ship-restrictions.txt`` is RSR telling us where **RSR** will not
send a given SKU. It is 517,560 lines over 21,795 SKUs, refreshed every two
hours — stored one row per SKU with the lines packed into JSON, because
every read here is "one SKU, one destination" and never anything else.

**This is display information, and it is never a checkout gate.** It was decided
on that explicitly: the file is RSR's policy about RSR's own shipping, not
a legal determination — it says nothing about our own vehicles, our licence, or
our judgement, and the compliance call stays with the shop. Nothing in this
module may be wired into a cart, a checkout, or an order-validation throw.

What it IS for: not handing RSR an order they are certain to reject. A confirmed
hit means "do not AUTO drop-ship" and surfaces a To-Store route instead, which is
an operations decision, not a compliance one.

Three rules earn their own code below, in rising order of how badly a shortcut
would bite:

1. **The three levels are a union.** City, county and state restrictions are
   OR-ed. A state with no row does not clear a city that has one.

2. **Blocking and permit codes are different things.** Four codes (Y/F/A/L) say
   RSR will not fulfil or drop-ship. Four (H/O/S/R) say the RECIPIENT needs a
   permit and say nothing about shipping — they must be SHOWN without withholding
   the drop-ship recommendation, or 14,104 rows of shippable goods get refused
   for no reason. Each match row therefore carries its own ``blocks_dropship``.

3. **Codes we cannot read still fail CLOSED**, even though that set is currently
   EMPTY. RSR has already revised this layout document once — the copy on the FTP
   is an older four-code revision — so a ninth code must arrive as "we cannot
   read this, so it blocks", never as "no restriction". Same for an unrecognised
   ``Territory Restriction Type``: an unreadable SCOPE is not a pass either.

4. **County is a third answer, not a rounding error.** RSR resolves county by
   reverse-lookup from the zip (manual p.66). A Woo order carries city, state and
   zip, and we have no zip->county dataset, so a ``T`` row can be neither
   confirmed nor discarded. Reporting it as a hit would let one restricted county
   (Cook) embargo an entire state; dropping it would silently clear 75,207 rows of
   real restriction. It is reported as ``county_uncertain`` and rendered as "this
   state has county-level restrictions and we could not resolve the destination
   county". When a zip->county dataset lands, only this branch changes.

Operator-facing wording is produced HERE, on the server, so the POS and the WP
metabox cannot drift into two different explanations of the same code.
"""
from __future__ import annotations

import frappe

# ---------------------------------------------------------------- vocabulary

# Territory Restriction Type (column 4) — keydealer/restrictions-fields.txt.
# NB: C is City and T is County. An earlier reading of this feed guessed
# C=County/T=Town; a later change corrects it. Do not "fix" these back.
TERRITORY_CITY = "C"
TERRITORY_COUNTY = "T"
TERRITORY_STATE = "S"

LEVEL_CITY = "city"
LEVEL_COUNTY = "county"
LEVEL_STATE = "state"

_TERRITORY_LEVEL = {
	TERRITORY_CITY: LEVEL_CITY,
	TERRITORY_COUNTY: LEVEL_COUNTY,
	TERRITORY_STATE: LEVEL_STATE,
}

# Most specific first — `level` reports the narrowest CONFIRMED hit.
_LEVEL_ORDER = (LEVEL_CITY, LEVEL_STATE)

# ------------------------------------------------------- Restriction Detail (5)
#
# **Source: the CURRENT layout revision, from Dealer's Toolbox -> Available
# Downloads.** The copy sitting on the FTP as
# `keydealer/restrictions-fields.txt` is an OLDER revision listing only four codes
# — reading that one is how S/R/H/O came to be called undocumented twice.
#
# The current revision defines all eight, and the wording splits them cleanly in
# two. Four state a CONSEQUENCE:
#
#   Y  "All shipments into the defined area are blocked"
#   F  "All fulfillment or drop shipments into the defined area are blocked"
#   A  "...require an affidavit signed by the dealer AND ARE BLOCKED TO
#       FULFILLMENT OR DROP SHIPMENTS"
#   L  "...require a license AND ARE BLOCKED TO FULFILLMENT OR DROP SHIPMENTS"
#
# ...and four state a REQUIREMENT ON THE RECIPIENT and stop there — no blocking
# clause at all:
#
#   H  "All shipments into the defined area require a Hi-capacity permit"
#   O  "All shipments into the defined area require a SOTS licensing"
#   S  "All shipments into the defined area require an assault weapon permit"
#   R  "All shipments into the defined area require a short-barrel permit"
#
# A second, independent reading agrees: the companion-F note ("if an item has A,
# L or Y there will also be a record for F") names those three and not these four.
#
# ⚠ The two code columns share letters with UNRELATED meanings. Column 4 ``S`` is
# the State scope; column 5 ``S`` is the assault-weapon permit. Separate constants
# and separate lookup tables deliberately — merging them produces output that
# looks entirely plausible and is wrong.
DETAIL_ALL_SHIPPING_BLOCKED = "Y"
DETAIL_DROPSHIP_BLOCKED = "F"
DETAIL_AFFIDAVIT_REQUIRED = "A"
DETAIL_LICENSE_REQUIRED = "L"
DETAIL_HICAP_PERMIT = "H"
DETAIL_SOTS_LICENSING = "O"
DETAIL_ASSAULT_WEAPON_PERMIT = "S"   # NOT the State scope — see TERRITORY_STATE
DETAIL_SHORT_BARREL_PERMIT = "R"

# The four whose own definition says RSR will not fulfil or drop-ship.
_BLOCKING_CODES = frozenset({
	DETAIL_ALL_SHIPPING_BLOCKED, DETAIL_DROPSHIP_BLOCKED,
	DETAIL_AFFIDAVIT_REQUIRED, DETAIL_LICENSE_REQUIRED,
})

# The four that require paperwork of the RECIPIENT and say nothing about shipping.
# They DISPLAY (the shop has to hold the permit) and they do NOT block: reading
# them as blockers would withhold the drop-ship recommendation on 14,104 rows of
# perfectly shippable goods.
PERMIT_CODES = frozenset({
	DETAIL_HICAP_PERMIT, DETAIL_SOTS_LICENSING,
	DETAIL_ASSAULT_WEAPON_PERMIT, DETAIL_SHORT_BARREL_PERMIT,
})

DOCUMENTED_DETAIL_CODES = _BLOCKING_CODES | PERMIT_CODES

# Codes RSR ships that no revision of the layout document defines. **Currently
# empty** — all eight live values are accounted for above. The constant and the
# fail-closed branch behind it stay because RSR has already revised this document
# once: a ninth code must arrive as "we cannot read this, so it blocks", never as
# "no restriction". A non-empty set here means someone found a new code and should
# go read the current Dealer's Toolbox revision.
UNDOCUMENTED_DETAIL_CODES_SEEN: frozenset = frozenset()

# Operator-facing wording. The two families are worded DIFFERENTLY on purpose: the
# first four say RSR will not send it, the last four say a permit is needed and
# never imply a refusal. And each permit is NAMED — "requires SOTS licensing"
# tells an operator what to do next, "restricted" tells them nothing.
_DETAIL_TEXT = {
	DETAIL_DROPSHIP_BLOCKED:
		"RSR will not drop-ship this item to {place}.",
	DETAIL_ALL_SHIPPING_BLOCKED:
		"RSR blocks every shipment of this item to {place}, drop-ship included.",
	DETAIL_AFFIDAVIT_REQUIRED:
		"RSR needs a dealer-signed affidavit for this item in {place}, "
		"and will not drop-ship it there.",
	DETAIL_LICENSE_REQUIRED:
		"RSR needs a licence on file for this item in {place}, "
		"and will not drop-ship it there.",
	DETAIL_HICAP_PERMIT:
		"Shipping this item to {place} requires a high-capacity permit.",
	DETAIL_SOTS_LICENSING:
		"Shipping this item to {place} requires SOTS licensing.",
	DETAIL_ASSAULT_WEAPON_PERMIT:
		"Shipping this item to {place} requires an assault-weapon permit.",
	DETAIL_SHORT_BARREL_PERMIT:
		"Shipping this item to {place} requires a short-barrel permit.",
}


def blocks_dropship(code: str) -> bool:
	"""Does one restriction code withhold the drop-ship recommendation?

	Decided PER CODE, from that code's own definition — deliberately NOT as "is
	there an ``F`` in the set". The two rules agree on well-formed data, because
	RSR promises a companion ``F`` alongside A/L/Y, and the short one is tempting
	because it is a single membership test.

	They are not the same rule. "Contains F" makes our verdict depend on RSR's
	data being internally consistent, when each code's own text already answers
	the question: ``Y`` is "all shipments into the defined area are blocked", and
	a drop-ship is a shipment. On the day RSR emits a ``Y`` without its companion
	``F`` — a data error we cannot prevent and would not notice — the short rule
	drop-ships into an area RSR blocks outright. This one still refuses.

	Anything unrecognised (a blank, a ninth code) blocks: written as "not a code
	we can read means blocked" rather than a whitelist of blockers, so a newly
	documented code cannot slip through as permissive by default.
	"""
	code = (code or "").upper()
	if code in PERMIT_CODES:
		return False
	return code not in DOCUMENTED_DETAIL_CODES or code in _BLOCKING_CODES


def _undocumented_text(code: str, place: str) -> str:
	"""Wording for a code no revision of the layout document defines.

	Currently unreachable in production — all eight live codes are documented —
	and kept for the ninth. The distinction it draws is deliberate: "RSR ships
	this code and does not define it" sends someone to ask RSR, whereas
	"unrecognised code" sounds like OUR bug and sends them to read this file.
	"""
	seen = (" RSR's feed uses it and no revision of the layout document defines "
		"it.") if code.upper() in UNDOCUMENTED_DETAIL_CODES_SEEN else ""
	shown = code.upper() if code else "(blank)"
	return (f"RSR flags this item in {place} with restriction code "
		f"\"{shown}\", which RSR does not document.{seen} "
		"Treated as no-drop-ship until RSR confirms what it means. "
		"Check the current layout file on Dealer's Toolbox -> Available Downloads; "
		"the copy on the FTP has been out of date before.")


# ------------------------------------------------------------- normalisation

def _norm_city(value) -> str:
	"""Upper-cased, whitespace-collapsed. The feed is upper-case ("CHICAGO
	HEIGHTS"); a Woo address is whatever the buyer typed."""
	return " ".join(str(value or "").strip().upper().split())


def _norm_state(value) -> str:
	"""Two-letter comparison form, via ffl_core's normaliser so a spelled-out
	"Illinois" still matches an "IL" row — the same fail-open ffl_core's
	`_state_restricted` was fixed for. Falls back to an upper-cased exact
	compare for anything the normaliser does not know, so nothing that matched
	before stops matching."""
	raw = str(value or "").strip()
	if not raw:
		return ""
	try:
		from ffl_core.compliance.interstate_tax import normalize_us_state

		return (normalize_us_state(raw) or raw).upper()
	except Exception:  # noqa: BLE001 — a missing ffl_core must not blank the panel
		return raw.upper()


def _place(row_state: str, municipality: str, level: str) -> str:
	"""How the destination is named back to the operator."""
	muni = str(municipality or "").strip().strip('"')
	if level == LEVEL_CITY:
		return f"{muni.title()}, {row_state}" if muni else row_state
	if level == LEVEL_COUNTY:
		return f"{muni.title()} County, {row_state}" if muni else row_state
	return row_state


# ------------------------------------------------------------------ pure core

def evaluate(rows, *, state, city) -> dict:
	"""Judge one SKU against one destination. Pure: rows in, verdict out.

	``rows`` are raw restriction lines (``municipality`` / ``state`` /
	``territory_type`` / ``restriction_detail``) — normally already filtered to
	this SKU and state by :func:`restrictions_for`, but rows for other states are
	re-checked and ignored here so a caller passing a whole SKU's rows cannot get
	New York's bans applied to a Texas address.

	Returns, on every path (the WP metabox renders this dict and must never have
	to guess whether a missing key means "false" or "unknown"):

	``checked``           False when the destination state is blank — nothing was
	                      evaluated, and ``blocked_dropship`` is None, not False.
	``blocked_dropship``  True on a CONFIRMED hit -> do not auto drop-ship.
	                      None when ``checked`` is False.
	``codes``             Documented raw letters on confirmed hits, sorted.
	``unknown_codes``     Undocumented raw letters on confirmed hits, sorted.
	``level``             Narrowest confirmed level, "city" or "state", else None.
	                      Never "county" — v1 cannot confirm one.
	``levels``            Every confirmed level, narrowest first.
	``county_uncertain``  The state has county rows we could not resolve.
	``city_uncertain``    The state has city rows and the order carries no city.
	``matches``           Display rows for confirmed hits.
	``uncertain``         Display rows for hits we could not resolve.
	"""
	dest_state = _norm_state(state)
	if not dest_state:
		# Checked BEFORE anything else, and answered with None rather than False:
		# an unresolved destination is the ORDINARY case on this panel (the FFL
		# premise state is blank until the FFL is captured). Answering False would
		# clear a line nobody ever checked — the same tri-state discipline
		# options.py already applies to `restricted_state`.
		return _verdict(checked=False)

	dest_city = _norm_city(city)
	# What we actually judged against, echoed back: when an answer surprises an
	# operator, the address it was judged against is almost always why.
	destination = f"{dest_city.title()}, {dest_state}" if dest_city else dest_state
	matches: list[dict] = []
	uncertain: list[dict] = []
	county_uncertain = False
	city_uncertain = False

	for row in rows or []:
		if not row:
			continue  # a malformed line degrades the panel, never takes it down
		row_state = _norm_state(row.get("state"))
		if row_state != dest_state:
			continue

		raw_code = str(row.get("restriction_detail") or "").strip()
		code = raw_code.upper()
		documented = code in DOCUMENTED_DETAIL_CODES
		municipality = str(row.get("municipality") or "").strip().strip('"')
		territory = str(row.get("territory_type") or "").strip().upper()
		# An unrecognised scope is applied at STATE breadth: it is the widest scope
		# the row can honestly claim (the row does name a state), and treating it as
		# narrower would be a guess in the permissive direction. `scope_documented`
		# carries the truth to the operator.
		level = _TERRITORY_LEVEL.get(territory, LEVEL_STATE)
		scope_documented = territory in _TERRITORY_LEVEL

		entry = {
			"level": level,
			"code": code or "",
			"documented": documented,
			"scope_documented": scope_documented,
			"municipality": municipality,
			"state": row_state,
		}

		if level == LEVEL_COUNTY:
			# Rule 3: never confirmable in v1, never dropped either.
			county_uncertain = True
			entry["text"] = (
				f"RSR restricts this item in {_place(row_state, municipality, level)}"
				f" (code \"{code or '(blank)'}\""
				f"{'' if documented else ', which RSR does not document'}). "
				"This order carries no county, so we cannot tell whether "
				f"{dest_city.title() or 'the destination'} falls inside it — "
				"confirm before drop-shipping.")
			uncertain.append(entry)
			continue

		if level == LEVEL_CITY:
			if not dest_city:
				city_uncertain = True
				entry["text"] = (
					f"RSR restricts this item in "
					f"{_place(row_state, municipality, level)}. This order carries "
					"no city, so this could not be checked.")
				uncertain.append(entry)
				continue
			if _norm_city(municipality) != dest_city:
				continue  # a different city's ban is not this destination's problem

		place = _place(row_state, municipality, level)
		entry["blocks_dropship"] = blocks_dropship(code)
		entry["text"] = (_DETAIL_TEXT[code].format(place=place) if documented
			else _undocumented_text(code, place))
		if not scope_documented:
			entry["text"] += (
				f" RSR's scope code for this row (\"{territory or '(blank)'}\") is "
				"also undocumented; it is applied state-wide to be safe.")
		matches.append(entry)

	codes = sorted({m["code"] for m in matches if m["documented"]})
	unknown_codes = sorted({m["code"] for m in matches if not m["documented"]})
	levels = [lvl for lvl in _LEVEL_ORDER if any(m["level"] == lvl for m in matches)]
	return _verdict(
		checked=True,
		# Not `bool(matches)`: a confirmed restriction and a drop-ship block are two
		# different facts now that the permit family (S/R/H) exists. "RSR needs an
		# AWL here" must be SHOWN whether or not it also stops the shipment.
		blocked_dropship=any(m["blocks_dropship"] for m in matches),
		codes=codes,
		unknown_codes=unknown_codes,
		level=levels[0] if levels else None,
		levels=levels,
		county_uncertain=county_uncertain,
		city_uncertain=city_uncertain,
		matches=matches,
		uncertain=uncertain,
		destination=destination,
	)


def _verdict(*, checked, blocked_dropship=None, codes=None, unknown_codes=None,
		level=None, levels=None, county_uncertain=False, city_uncertain=False,
		matches=None, uncertain=None, destination="") -> dict:
	"""One constructor so every return path carries every key."""
	return {
		"checked": bool(checked),
		"blocked_dropship": blocked_dropship if checked else None,
		"codes": codes or [],
		"unknown_codes": unknown_codes or [],
		"level": level,
		"levels": levels or [],
		"county_uncertain": bool(county_uncertain),
		"city_uncertain": bool(city_uncertain),
		"matches": matches or [],
		"uncertain": uncertain or [],
		"destination": destination,
		"headline": _headline(
			checked=checked,
			# "blocks drop-ship" and "has a restriction" are two different facts
			# now that the permit family exists — the headline needs both.
			blocked=any(m.get("blocks_dropship") for m in (matches or [])),
			restricted=bool(matches),
			destination=destination,
			county_uncertain=county_uncertain, city_uncertain=city_uncertain,
			level=level, unknown_codes=unknown_codes or []),
	}


def _headline(*, checked, blocked, restricted, destination, county_uncertain,
		city_uncertain, level, unknown_codes) -> str:
	"""The one line the metabox shows before anyone expands the detail.

	Served from here rather than assembled in the renderer so the POS and the WP
	console cannot drift into two different explanations of the same code — the
	same rule the rest of this project already follows for operator wording.

	Empty string on a clean destination: there is nothing to say, and an empty
	headline is how the renderer knows not to draw the banner at all.
	"""
	where = destination or "this destination"
	if not checked:
		return (f"RSR ship restrictions could not be checked for {where} — "
			"the destination is not resolved yet.")
	if blocked:
		text = f"RSR will not drop-ship this item to {where}"
		if level:
			text += f" ({level}-level restriction)"
		text += "."
		if unknown_codes:
			text += (" Some of the codes involved are undocumented by RSR and are "
				"treated as no-drop-ship.")
		if county_uncertain:
			text += (" This state also has county-level restrictions that could not "
				"be resolved.")
		return text
	if restricted:
		# Confirmed restrictions that do NOT block drop-ship — the permit family
		# with the switch off. The operator still has to act on them.
		return (f"RSR records a permit requirement on this item for {where}. It does "
			"not block drop-ship, but check the paperwork before shipping.")
	if county_uncertain:
		return (f"This state has county-level RSR restrictions on this item. The "
			f"order carries no county, so we cannot tell whether {where} is "
			"affected — confirm before drop-shipping.")
	if city_uncertain:
		return (f"This state has city-level RSR restrictions on this item and the "
			f"order carries no city, so {where} could not be checked.")
	return ""


# -------------------------------------------------------------- remote reads

def _entries_for(rsr_stock_number) -> list:
	"""One SKU's raw restriction entries — a list of ``[municipality, state,
	territory_type, restriction_detail]`` 4-lists, verbatim from the feed, now
	read through the remote catalog (Data Service) rather than the local batch table.

	``[]`` when the stock number is blank or the SKU has no row. Transport
	failure RAISES — :func:`check` catches it and reports "could not check",
	which is the honest degradation (a swallowed parse error here would render
	as "no restrictions"); :func:`blocked_states_for` catches it too and quotes
	``[]``, same contract as before the repoint."""
	stock = str(rsr_stock_number or "").strip()
	if not stock:
		return []
	from ffl_integrations.distributor import remote_catalog
	return remote_catalog.restrictions_entries("rsr", stock)


def restrictions_for(rsr_stock_number, *, state) -> list:
	"""Every restriction row for one SKU in one state, read through the remote
	catalog (:func:`_entries_for`).

	The state filter runs here in Python — a SKU averages 24 entries, so there is
	nothing for SQL to win. Row dicts keep the per-line field names the pure
	:func:`evaluate` layer is written against."""
	dest_state = _norm_state(state)
	if not dest_state:
		return []
	return [
		{"municipality": municipality, "state": row_state,
			"territory_type": territory, "restriction_detail": detail}
		for municipality, row_state, territory, detail
		in _entries_for(rsr_stock_number)
		if row_state == dest_state]


def blocked_states_for(rsr_stock_number) -> list:
	"""Sorted 2-letter states where ANY of this SKU's entries blocks drop-ship,
	at any territory level — the quote panel's red line.

	Deliberately coarse: a Chicago-only ``F`` reports "IL", because at typeahead
	time the honest summary of "RSR blocks drop-ship somewhere in this state" IS
	the state; the order screen's :func:`check` resolves the exact destination.
	Display only, like everything in this module — never a gate. ``[]`` on any
	failure: this is a hint beside the authoritative panel, and a hint that can
	take down a quote would be a bad trade."""
	try:
		return sorted({
			row_state
			for _municipality, row_state, _territory, detail
			in _entries_for(rsr_stock_number)
			if row_state and blocks_dropship(detail)})
	except Exception:  # noqa: BLE001 — display hint; the panel still judges
		frappe.log_error(
			title=f"RSR ship restrictions: blocked-states read failed "
				f"({rsr_stock_number})",
			message=frappe.get_traceback())
		return []


def check(rsr_stock_number, *, state, city) -> dict:
	"""``restrictions_for`` + :func:`evaluate` — the one call the metabox path uses.

	Never raises: a restriction panel that 500s takes the whole fulfilment screen
	with it, and this is decoration on that screen, not its gate. A failure is
	logged and reported as unchecked, which the UI renders as "could not check" —
	never as "no restrictions".
	"""
	try:
		rows = restrictions_for(rsr_stock_number, state=state)
	except Exception:  # noqa: BLE001
		frappe.log_error(
			title=f"RSR ship restrictions: read failed ({rsr_stock_number})",
			message=frappe.get_traceback())
		return _verdict(checked=False)
	return evaluate(rows, state=state, city=city)
