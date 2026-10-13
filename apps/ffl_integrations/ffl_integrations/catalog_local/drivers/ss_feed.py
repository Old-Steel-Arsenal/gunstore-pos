"""Sports South feed parsers, streaming over a downloaded inventory.asmx response.

Every response is ``<string xmlns=...>`` whose text is the XML-escaped inner
document. Step 0 sniffs an ``<ERROR>`` payload in the first 4 KB. Step 1 unwraps
the envelope to a second temp file (one unescape pass, XML-illegal character
references dropped). Step 2 iterparses that file in constant memory.

Timestamps are echoed verbatim: an inventory.asmx time is a Central wall clock
with a fake +00:00 suffix, so nothing here converts it.

Ported from the Data Service's Sports South parser. The class rule at the
bottom is the Data Service's Sports South product_class rule with its two
Sports South owner rules applied, so every row gets the class the Data Service
serves for it.
"""
from __future__ import annotations

import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import frappe

# The Data Service's NFA title words, shared with the RSR driver. The owner rule
# for category 93 checks the bare word rule _NFA_TITLE, without the not-an-NFA-item
# exceptions, exactly as the Data Service does.
from ffl_integrations.catalog_local.drivers.rsr_feed import _NFA_TITLE, nfa_title
from ffl_integrations.catalog_local.sync import FeedAuthError, FeedFormatError
from ffl_integrations.distributor.ss_provider import (
	AMMO_CATEGORIES,
	HANDGUN_CATEGORIES,
	LONG_GUN_CATEGORIES,
	OTHER_FIREARM_CATEGORIES,
	RECEIVER_CATEGORIES,
	SUPPRESSOR_CATEGORIES,
)
from ffl_integrations.sports_south.api_client import is_auth_error

MEDIA_BASE = "https://media.server.theshootingwarehouse.com"

# Required on every DailyItemUpdate row. A row missing any of these is rejected.
REQUIRED_ITEM_FIELDS = ("ITEMNO", "ITYPE", "CATID", "CPRC", "QTYOH", "ITUPC")

# Rejected-row ratio gate: only meaningful on a real population.
REJECT_RATIO = 0.01
REJECT_RATIO_FLOOR = 5_000

# Serialized-firearm discriminator is row-level (item type), never category.
GUN_ITYPES = frozenset({"1", "2"})

# Category attribute labels that mean "chambering".
CALIBER_LABELS = frozenset({"caliber", "gauge", "caliber or gauge"})

# Slot naming quirk of the vendor: slot 10 is ATTR0 / ITATR0.
_ATTR_SLOTS = (*range(1, 10), 0, *range(11, 21))

_ILLEGAL_REF = re.compile(r"&#(x[0-9a-fA-F]+|[0-9]+);")

UNFLAGGED_PART_CATEGORIES = frozenset({"8"}) | RECEIVER_CATEGORIES | SUPPRESSOR_CATEGORIES

# An owner rule of the Data Service: category 31 (lights) holds no guns, whatever the flag says.
# A light is an Accessory here whatever its flag or title (the class the Data Service serves
# for it), and the local backend reads the same set through the driver for the product's legal answer.
NEVER_FIREARM_CATEGORIES = frozenset({"31"})

# The Data Service's other Sports South owner rule: a flagged row in the suppressor category (93)
# whose title says pistol, and names no NFA item, is a pistol, not an NFA item.
PISTOL_TITLE = re.compile(r"\bpistols?\b", re.I)


def _log():
	return frappe.logger("catalog_local", allow_site=True)


def unlink_quiet(path) -> None:
	if not path:
		return
	try:
		os.unlink(path)
	except OSError:
		pass


def image_urls(picref: str, itemno: str) -> tuple[str, str]:
	"""(standard, hires) media-server URLs. PICREF keys the image; ITEMNO is the fallback."""
	ref = picref or itemno
	return (f"{MEDIA_BASE}/large/{ref}.jpg", f"{MEDIA_BASE}/hires/{ref}.png")


def assert_no_error(path: str | Path) -> None:
	"""Step 0: refuse error payloads before any real parsing."""
	with open(path, "rb") as fh:
		head = fh.read(4096)  # the first bytes only: the snapshot file runs to hundreds of MB
	if b"&lt;ERROR&gt;" in head:
		m = re.search(rb"&lt;ERROR&gt;(.*?)&lt;/ERROR&gt;", head, re.S)
		msg = (m.group(1).decode("utf-8", "replace").strip() if m else "") or "unknown error"
		if is_auth_error(msg):
			raise FeedAuthError(f"Sports South rejected the login ({msg})")
		raise FeedFormatError(f"Sports South error payload: {msg}")


def _drop_illegal_refs(s: str) -> str:
	def _keep(m: re.Match) -> str:
		body = m.group(1)
		cp = int(body[1:], 16) if body[0] in "xX" else int(body)
		# XML 1.0 Char production: controls out (except tab, lf, cr), surrogates out, FFFE/FFFF out.
		legal = (cp in (0x9, 0xA, 0xD)
				 or (0x20 <= cp <= 0xD7FF)
				 or (0xE000 <= cp <= 0xFFFD)
				 or (0x10000 <= cp <= 0x10FFFF))
		return m.group(0) if legal else ""
	return _ILLEGAL_REF.sub(_keep, s)


def _unescape_outer(s: str) -> str:
	# The five XML entities only. &amp; goes last so inner entities stay escaped for iterparse.
	return (s.replace("&lt;", "<").replace("&gt;", ">")
			 .replace("&quot;", '"').replace("&apos;", "'")
			 .replace("&amp;", "&"))


def unwrap_to_file(path: str | Path) -> str:
	"""Stream the raw response into a parseable inner document. Returns the temp file path;
	the caller unlinks it. A carry buffer keeps entities and character references from
	being split across chunks."""
	fd, out_path = tempfile.mkstemp(prefix="ss-inner-", suffix=".xml")
	try:
		with open(path, encoding="utf-8", newline="") as src, \
			 os.fdopen(fd, "w", encoding="utf-8") as out:
			# Seek past the <string ...> envelope open tag.
			head = src.read(512)
			start = head.find("<string")
			gt = head.find(">", start) if start >= 0 else -1
			if gt < 0:
				raise FeedFormatError("missing <string> envelope")
			carry = head[gt + 1:]
			while True:
				chunk = src.read(1 << 20)
				if not chunk:
					break
				buf = carry + chunk
				# Hold back a tail window: it may hold a split entity or character reference,
				# or the closing </string>, which is always raw.
				cut = len(buf) - 48
				if cut <= 0:
					carry = buf
					continue
				amp = buf.rfind("&", max(0, cut - 40), cut)
				if amp != -1 and buf.find(";", amp, cut) == -1:
					cut = amp
				carry = buf[cut:]
				out.write(_drop_illegal_refs(_unescape_outer(buf[:cut])))
			end = carry.rfind("</string>")
			if end != -1:
				carry = carry[:end]
			out.write(_drop_illegal_refs(_unescape_outer(carry)))
	except Exception:
		unlink_quiet(out_path)
		raise
	return out_path


def iter_records(inner_path: str | Path, record_tag: str = "Table"):
	"""Yield {field: stripped_text} per record element, constant memory. A truncated
	document makes iterparse raise ParseError before the loop finishes."""
	root = None
	for event, elem in ET.iterparse(str(inner_path), events=("start", "end")):
		if event == "start":
			if root is None:
				root = elem
			continue
		if elem.tag == record_tag:
			yield {c.tag: (c.text or "").strip() for c in elem}
			root.clear()


def parse_categories(path: str | Path):
	"""CategoryUpdate -> (names, slots). names: catid -> display name with the parent
	department inline. slots: catid -> tuple of ITATR field names whose label is a chambering."""
	assert_no_error(path)
	inner = unwrap_to_file(path)
	try:
		names, slots = {}, {}
		for r in iter_records(inner):
			catid = r.get("CATID", "")
			if not catid:
				continue
			dep = r.get("DEP", "") or r.get("DEPID", "")
			catdes = r.get("CATDES", "")
			names[catid] = f"{catdes} ({dep})" if dep else catdes
			hit = tuple(
				f"ITATR{i}" for i in _ATTR_SLOTS
				if r.get(f"ATTR{i}", "").strip().lower() in CALIBER_LABELS
			)
			if hit:
				slots[catid] = hit
		if not names:
			raise FeedFormatError("CategoryUpdate returned no categories")
		return names, slots
	finally:
		unlink_quiet(inner)


def parse_brands(path: str | Path) -> dict[str, str]:
	"""BrandUpdate -> {BRDNO: BRDNM}."""
	assert_no_error(path)
	inner = unwrap_to_file(path)
	try:
		return {r["BRDNO"]: r.get("BRDNM", "") for r in iter_records(inner) if r.get("BRDNO")}
	finally:
		unlink_quiet(inner)


def product_class(department, title, ffl_required) -> str:
	"""The class the Data Service serves for a Sports South row. Its two owner rules come
	first: a light (category 31) is an Accessory whatever its flag or title, and a flagged
	row in the suppressor category whose title says pistol, with no NFA word, is a Handgun.
	After that the department decides and an NFA title marks NFA, in the order of the
	Data Service's class rule."""
	dept = str(department or "").strip()
	title = title or ""
	ffl = bool(ffl_required)
	if dept in NEVER_FIREARM_CATEGORIES:
		return "Accessory"
	if dept in SUPPRESSOR_CATEGORIES and ffl and PISTOL_TITLE.search(title) and not _NFA_TITLE.search(title):
		return "Handgun"
	if dept in SUPPRESSOR_CATEGORIES and ffl:
		return "NFA"
	if ffl and nfa_title(title):
		return "NFA"
	if dept in HANDGUN_CATEGORIES:
		return "Handgun"
	if dept in LONG_GUN_CATEGORIES:
		return "Long Gun"
	if dept in OTHER_FIREARM_CATEGORIES:
		return "Other Firearm"
	if dept in RECEIVER_CATEGORIES and ffl:
		return "Other Firearm"
	if dept in UNFLAGGED_PART_CATEGORIES and not ffl:
		return "Gun Parts"
	if ffl:
		return "Other Firearm"
	if dept in AMMO_CATEGORIES:
		return "Ammunition"
	return "Accessory"


def iter_catalog(path, *, brands, department_names, caliber_slots, stats=None, present=None):
	"""DailyItemUpdate -> catalog row dicts, one at a time. ``present``, when given, collects
	the ITEMNO of every row in the feed, including rejected rows, so a refused row is never
	treated as discontinued. The rejected-row gate raises at end of stream."""
	assert_no_error(path)
	inner = unwrap_to_file(path)
	total = rejected = 0
	try:
		for r in iter_records(inner):
			total += 1
			if present is not None and r.get("ITEMNO"):
				present.add(r["ITEMNO"])
			if any(f not in r or r[f] == "" for f in REQUIRED_ITEM_FIELDS):
				rejected += 1
				continue
			try:
				qty = int(float(r["QTYOH"]))
				cost = float(r["CPRC"])
				mfprc = float(r.get("MFPRC") or 0.0)
				weight = float(r.get("WTPBX") or 0.0)
			except ValueError:
				rejected += 1
				continue
			sku = r["ITEMNO"]
			catid, itype = r["CATID"], r["ITYPE"]
			# ffl_required is the house's own row flag (item type 1/2). There is no confirmed
			# antique list here, so every black-powder gun in category 13 stays FFL-required.
			ffl = itype in GUN_ITYPES
			policy = r.get("MFPRTYP", "")
			picref = r.get("PICREF", "")
			image_url, image_url_hires = image_urls(picref, sku)
			caliber = ""
			for slot in caliber_slots.get(catid, ()):
				if r.get(slot, ""):
					caliber = r[slot]
					break
			yield {
				"sku": sku,
				"upc": r["ITUPC"],
				"manufacturer": brands.get(r.get("ITBRDNO", ""), ""),
				"model": r.get("IMODEL", ""),
				"department": catid,
				"department_name": department_names.get(catid, ""),
				"cost": cost,
				"map": mfprc if policy == "M" else 0.0,
				"msrp": mfprc if policy == "U" else 0.0,
				"map_policy": policy,
				"qty": qty,
				"ffl_required": ffl,
				"dropship_blocked": False,
				"vendor_approval_required": False,
				"image_url": image_url,
				"discontinued": False,
				"status_flag": "",
				"short_description": r.get("SHDESC", ""),
				"mfg_number": r.get("MFGINO", ""),
				"caliber": caliber,
				"image_name": picref or sku,
				"image_url_hires": image_url_hires,
				"weight_lb": weight,
				"prop65": False,
				"ships_ground_only": r.get("HAZAIR", "") == "Y",
				"signature_required": False,
				"product_class": product_class(catid, r.get("SHDESC", ""), ffl),
			}
		if stats is not None:
			stats["total"] = total
			stats["rejected"] = rejected
		if total >= REJECT_RATIO_FLOOR and rejected > total * REJECT_RATIO:
			raise FeedFormatError(
				f"Sports South catalog: {rejected}/{total} rows rejected "
				f"(more than {REJECT_RATIO:.0%}) - refusing the feed")
		if rejected:
			_log().warning("Sports South catalog: %d/%d rows rejected (below gate, counted only)",
						   rejected, total)
	finally:
		unlink_quiet(inner)


def _onhand_row(r: dict) -> tuple[str, int, float | None] | None:
	"""One onhand record -> (sku, qty, cost), or None when it must be rejected. A blank cost
	is None, so the stored cost is kept instead of being replaced by a fabricated zero."""
	if not r.get("I") or not r.get("Q"):
		return None
	try:
		qty = int(float(r["Q"]))
		cost = float(r["C"]) if r.get("C") else None
	except ValueError:
		return None
	return (r["I"], qty, cost)


def _guard_onhand_rejects(label: str, total: int, rejected: int) -> None:
	if rejected:
		_log().warning("%s: %d/%d rows rejected (missing or invalid I/Q/C)", label, rejected, total)
	# A wholly rejected window is a format failure, never data.
	if total > 0 and rejected == total:
		raise FeedFormatError(f"{label}: all {total} rows rejected - refusing the window")
	if total >= REJECT_RATIO_FLOOR and rejected > total * REJECT_RATIO:
		raise FeedFormatError(
			f"{label}: {rejected}/{total} rows rejected (more than {REJECT_RATIO:.0%}) - refusing the window")


def parse_onhand_incremental(path: str | Path, stats: dict | None = None):
	"""IncrementalOnhandUpdate -> (SERVERTIME verbatim, [(sku, qty, cost)]). A lone SERVERTIME
	means no changes. A bare empty dataset is an idle window with no cursor."""
	assert_no_error(path)
	inner = unwrap_to_file(path)
	total = rejected = 0
	try:
		servertime = ""
		# lazy: an incremental window is small and held as a list; the full refresh streams
		rows: list[tuple[str, int, float | None]] = []
		root = None
		for event, elem in ET.iterparse(str(inner), events=("start", "end")):
			if event == "start":
				if root is None:
					root = elem
				continue
			if elem.tag == "SERVERTIME":
				servertime = (elem.text or "").strip()
			elif elem.tag == "Onhand":
				r = {c.tag: (c.text or "").strip() for c in elem}
				root.clear()
				total += 1
				row = _onhand_row(r)
				if row is None:
					rejected += 1
					continue
				rows.append(row)
		if stats is not None:
			stats["total"] = total
			stats["rejected"] = rejected
		_guard_onhand_rejects("Sports South onhand delta", total, rejected)
		if not servertime:
			# An empty buffer is a bare <NewDataSet />, with no ServerDateTime row at all.
			if not rows and total == 0:
				return "", []
			raise FeedFormatError(
				"IncrementalOnhandUpdate carried rows but no ServerDateTime - refusing the window")
		return servertime, rows
	finally:
		unlink_quiet(inner)


def iter_onhand_full(path: str | Path, stats: dict | None = None):
	"""OnhandUpdate (no parameters) -> (sku, qty, cost) for the whole active catalog, streamed.
	An empty document is refused: a full refresh with zero rows would wipe every quantity."""
	assert_no_error(path)
	inner = unwrap_to_file(path)
	total = rejected = 0
	try:
		for r in iter_records(inner, "Table"):
			total += 1
			row = _onhand_row(r)
			if row is None:
				rejected += 1
				continue
			yield row
		if stats is not None:
			stats["total"] = total
			stats["rejected"] = rejected
		_guard_onhand_rejects("Sports South full on-hand", total, rejected)
		if total == 0:
			raise FeedFormatError("OnhandUpdate returned no rows - refusing an empty full refresh")
	finally:
		unlink_quiet(inner)


def iter_text_rows(path: str | Path):
	"""ListNewText -> (ITEMNO, TEXT) pairs. An empty text never overwrites existing copy."""
	assert_no_error(path)
	inner = unwrap_to_file(path)
	try:
		for r in iter_records(inner):
			if r.get("ITEMNO") and r.get("TEXT"):
				yield (r["ITEMNO"], r["TEXT"])
	finally:
		unlink_quiet(inner)
