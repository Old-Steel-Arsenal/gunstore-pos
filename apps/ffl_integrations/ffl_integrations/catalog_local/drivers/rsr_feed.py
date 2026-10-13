"""RSR feed parsers — streaming generators over a downloaded feed file.

A port of the Data Service's RSR feed parser (itself a port of the old in-POS RSR
loader): the same column positions, quoting and decoding, but every parser
yields one row at a time, so a feed never sits in memory as a list. The layout
guards run when the generator reaches the end of the file: the store stages
rows into a temporary table first, so a guard that raises there rolls the load
back before any live row is touched.

Decoding: the catalog, deleted and restrictions files are latin-1 (RSR's files
carry the odd non-UTF-8 byte); the quantity file is plain UTF-8 CSV.
"""
from __future__ import annotations

import csv
import hashlib
import re

from ffl_integrations.catalog_local import store
from ffl_integrations.catalog_local.sync import FeedFormatError
from ffl_integrations.setup.rsr_seed import DEPARTMENTS as _SEEDED_DEPARTMENTS

COL_RSR_STOCK_NUMBER = 0
COL_UPC = 1
COL_SHORT_DESCRIPTION = 2
COL_DEPARTMENT_NUMBER = 3
COL_MSRP = 5
COL_DEALER_PRICE = 6
COL_WEIGHT = 7
COL_QUANTITY = 8
COL_MODEL = 9
COL_MANUFACTURER = 10
COL_MFG_NUMBER = 11
COL_STATUS = 12
COL_LONG_DESCRIPTION = 13
COL_IMAGE_NAME = 14
# 15-65: per-state flags, deliberately not parsed — the ship-restrictions file
# is the state answer (display only, rsr/restrictions.py).
COL_SHIPS_GROUND_ONLY = 66
COL_SIGNATURE_REQUIRED = 67
COL_DROPSHIP_BLOCKED = 68
COL_MAP_PRICE = 70
COL_PROP65 = 75
COL_VENDOR_APPROVAL_REQUIRED = 76
CATALOG_COLUMN_COUNT = 77

# RSR's department table, as shipped in ``setup/rsr_seed.DEPARTMENTS`` (number,
# name, FFL, Item Group) — the same table the Data Service seeds
# (41/42/43 FFL, 07 Black Powder not). The FFL column is the flag; the name is
# kept for facets.
_DEPARTMENTS = {num: (name, bool(ffl)) for num, name, ffl, *_rest in _SEEDED_DEPARTMENTS}
DEPARTMENT_NAMES = {num: name for num, (name, _ffl) in _DEPARTMENTS.items()}
FFL_DEPARTMENTS = frozenset(num for num, (_name, ffl) in _DEPARTMENTS.items() if ffl)

# The four title rules of the Data Service's catalog classifier,
# verbatim (PG word bounds were already \b there). A FLASH SUPPRESSOR is a muzzle
# device, not an NFA item; a "SUPPRESSOR READY" pistol, a threaded barrel or an
# upper is not itself an NFA item. _RECEIVER/_NOT_RECEIVER find the regulated part
# of a gun (receiver, lower, frame, barreled action) but not uppers, parts kits,
# pins, springs, buffers. One local addition to _NOT_RECEIVER: RSR abbreviates an
# upper as "UP RCVR", which the Data Service never needed (its department rule keeps
# every 41-43 row a part).
_NFA_TITLE = re.compile(r"\b(sbr|sbs|aow|silencers?)\b|(?<!flash )(?<!flash-)\bsuppressors?\b"
	r"|short[- ]barrel(l?ed)? (rifle|shotgun)|any other weapon|machine ?gun", re.I)
_NOT_NFA_ITEM = re.compile(r"\b(suppressors?|silencers?|supp)[- ]?(ready|height|hgt|sights?|compatible)\b"
	r"|\bthreaded\b|\buppers?\b|(?<!short )(?<!short-)\bbarrels?\b", re.I)
_RECEIVER = re.compile(r"\blower receiver\b|\b(stripped|complete|cmplt|cmp|billet|forged)\.?\s+"
	r"(ar-?1[05]\s+|ar\s+)?lower\b|\blower\s+(rcvr|recvr)\b|\breceiver\b|\brcvr\b|"
	r"\bframe\b|\bbarrel(l)?ed action\b|\bbbl'?d action\b", re.I)
_NOT_RECEIVER = re.compile(r"\bupper|\buppr\b|\bupr\b|\bextension\b|\bparts\b|\bkit\b|\bpins?\b|vise|"
	r"\bblock\b|\bsprings?\b|\bscrews?\b|\bplate\b|\bbuffer|\bup\.?\s+(rcvr|recvr|receiver)\b", re.I)
_UPPERS = ("41", "42", "43")


def nfa_title(title: str) -> bool:
	"""The title names an NFA item (the Data Service's NFA word rule)."""
	title = title or ""
	return bool(_NFA_TITLE.search(title)) and not _NOT_NFA_ITEM.search(title)


def names_a_receiver(title: str) -> bool:
	"""The title names a receiver, lower, frame or barreled action (the Data Service's receiver rule)."""
	title = title or ""
	return bool(_RECEIVER.search(title)) and not _NOT_RECEIVER.search(title)


def department_key(department: str) -> str:
	"""RSR writes the number zero-padded ("01"); "1" is the same department."""
	num = (department or "").strip()
	return num.zfill(2) if num.isdigit() else num


def department_ffl(department: str) -> bool:
	"""The department table's FFL column. A blank or unlisted department is
	FFL-required — stricter than the Data Service, which defaults it to False."""
	num = department_key(department)
	return num in FFL_DEPARTMENTS or num not in _DEPARTMENTS


def product_class(department: str, title: str, ffl_required: bool) -> str:
	"""The RSR branch of the Data Service's product_class rule, in its order: the department
	decides and the title marks NFA. In the upper and conversion-kit departments
	(41/42/43) an FFL-flagged row whose title names a receiver, lower, frame or
	barreled action is a firearm (Other Firearm, or NFA when the title also names
	an NFA item), so legal_firearm() is True and the sale goes through the 4473 and
	the bound book. the Data Service's classifier has this receiver rule too, but a department
	rule there keeps every RSR 41/42/43 row a part; this port deliberately keeps the
	strict answer. A blank or unlisted department (FFL-required) lands on NFA or
	Other Firearm."""
	dept, title = department_key(department), title or ""
	if dept == "06":
		return "NFA"
	if ffl_required and nfa_title(title) and dept not in _UPPERS:
		return "NFA"
	if dept in ("01", "02"):
		return "Handgun"
	if dept in ("03", "05"):
		return "Long Gun"
	if dept == "18":
		return "Ammunition"
	if dept in _UPPERS:
		if ffl_required and names_a_receiver(title):
			return "NFA" if nfa_title(title) else "Other Firearm"
		return "Gun Parts"
	if dept in ("32", "34") and not ffl_required:
		return "Gun Parts"
	if ffl_required:
		return "Other Firearm"
	return "Accessory"


RESTRICTION_COLUMN_COUNT = 5
# More unreadable (or wrongly wide) lines than this fraction refuses the file.
PARSE_ERROR_FRACTION = 0.01

IMAGE_BASE = "https://img.rsrgroup.com/pimages"
IMAGE_BASE_HIRES = "https://img.rsrgroup.com/highres-pimages"

_TRUTHY = {"y", "yes", "1", "true"}


def _safe(row: list, col: int, max_len: int | None = None) -> str:
	if col >= len(row):
		return ""
	val = (row[col] or "").strip()
	return val[:max_len] if max_len else val


def _safe_int(row: list, col: int) -> int:
	try:
		return int(float(_safe(row, col) or 0))
	except (ValueError, TypeError):
		return 0


def _safe_float(row: list, col: int) -> float:
	try:
		return float(_safe(row, col) or 0)
	except (ValueError, TypeError):
		return 0.0


def _flag(row: list, col: int) -> bool:
	return _safe(row, col).lower() in _TRUTHY


def image_url(sku: str, image_name: str, *, hires: bool) -> str:
	if not sku:
		return ""
	stem = (image_name or f"{sku}_1").strip()
	if stem.lower().endswith((".jpg", ".jpeg", ".png")):
		stem = stem.rsplit(".", 1)[0]
	return f"{IMAGE_BASE_HIRES}/{stem}_HR.jpg" if hires else f"{IMAGE_BASE}/{stem}.jpg"


def _tilde_rows(path, what: str):
	"""``;``-separated, ``~``-quoted, latin-1 — RSR's convention on every file
	but restrictions. A csv error (a runaway quote) refuses the file."""
	with open(path, newline="", encoding="latin-1") as fh:
		reader = csv.reader(fh, delimiter=";", quotechar="~")
		try:
			yield from reader
		except csv.Error as exc:
			raise FeedFormatError(f"{what} file line {reader.line_num}: {exc}") from None


def _too_many(stats: dict, key: str) -> bool:
	total = stats.get("total", 0)
	return bool(total) and stats.get(key, 0) > total * PARSE_ERROR_FRACTION


def iter_catalog(path, stats: dict | None = None, deleted=frozenset()):
	"""The 77-column inventory file → one row dict per line, in the store's
	column names. A line of the wrong width (a column inserted or removed) or
	without a stock number is counted and skipped. At the end of the file: no
	data line at all, or more than 1% of either kind, raises FeedFormatError.
	``deleted``: stock numbers on RSR's deleted list read in the same job
	(store.norm_sku spelling); such a row is stored exactly as the deleted lane
	leaves it (Deleted, discontinued, qty 0), so the catalog never revives a SKU
	the deleted lane would flag again."""
	stats = {} if stats is None else stats
	for row in _tilde_rows(path, "catalog"):
		if not row:
			continue
		stats["total"] = stats.get("total", 0) + 1
		# RSR ends every line with ';' — 78 fields, the last one empty, is the
		# healthy layout (measured on the production file, the Data Service, 2026-08-01).
		if not (len(row) == CATALOG_COLUMN_COUNT
				or (len(row) == CATALOG_COLUMN_COUNT + 1 and row[-1] == "")):
			# a shifted row's fields sit at the wrong offsets: never written
			stats["wrong_width"] = stats.get("wrong_width", 0) + 1
			continue
		sku = _safe(row, COL_RSR_STOCK_NUMBER)
		if not sku:
			stats["skipped"] = stats.get("skipped", 0) + 1
			continue
		listed = store.norm_sku(sku) in deleted
		status = "Deleted" if listed else _safe(row, COL_STATUS)
		image_name = _safe(row, COL_IMAGE_NAME)
		title = _safe(row, COL_SHORT_DESCRIPTION, max_len=255)
		department = _safe(row, COL_DEPARTMENT_NUMBER)
		dept = department_key(department)
		ffl = department_ffl(department)
		yield {
			"sku": sku,
			"upc": _safe(row, COL_UPC),
			"short_description": title,
			"department": dept,
			"department_name": DEPARTMENT_NAMES.get(dept, ""),
			"manufacturer": _safe(row, COL_MANUFACTURER),
			"model": _safe(row, COL_MODEL),
			"mfg_number": _safe(row, COL_MFG_NUMBER),
			"long_description": _safe(row, COL_LONG_DESCRIPTION),
			"msrp": _safe_float(row, COL_MSRP),
			"cost": _safe_float(row, COL_DEALER_PRICE),
			"map": _safe_float(row, COL_MAP_PRICE),
			"map_policy": "",
			"weight_lb": _safe_float(row, COL_WEIGHT),
			"status_flag": status,
			# not orderable (Allocated, Deleted): published qty 0 (store.not_orderable)
			"qty": 0 if store.not_orderable(status) else _safe_int(row, COL_QUANTITY),
			"image_name": image_name,
			"image_url": image_url(sku, image_name, hires=False),
			"image_url_hires": image_url(sku, image_name, hires=True),
			"ships_ground_only": _flag(row, COL_SHIPS_GROUND_ONLY),
			"signature_required": _flag(row, COL_SIGNATURE_REQUIRED),
			"dropship_blocked": _flag(row, COL_DROPSHIP_BLOCKED),
			"vendor_approval_required": _flag(row, COL_VENDOR_APPROVAL_REQUIRED),
			"prop65": _flag(row, COL_PROP65),
			# In the catalog means live again, unless the deleted list names it.
			"discontinued": listed,
			"ffl_required": ffl,
			"product_class": product_class(department, title, ffl),
		}
	if not stats.get("total"):
		raise FeedFormatError("catalog file has no data lines — refusing it")
	if _too_many(stats, "skipped"):
		raise FeedFormatError(f"catalog layout mismatch: {stats['skipped']}/{stats['total']} "
			"lines unparseable")
	if _too_many(stats, "wrong_width"):
		raise FeedFormatError(f"catalog column layout shift: {stats['wrong_width']}/"
			f"{stats['total']} lines have the wrong field count (expected {CATALOG_COLUMN_COUNT})")


def iter_quantity(path):
	"""``IM-QTY-CSV.csv`` → ``(sku, qty)``; unreadable lines are skipped."""
	with open(path, newline="", encoding="utf-8") as fh:
		reader = csv.reader(fh)
		try:
			for row in reader:
				if len(row) < 2:
					continue
				sku = row[0].strip()
				if not sku:
					continue
				try:
					qty = int(float(row[1].strip() or 0))
				except (ValueError, TypeError):
					continue
				yield (sku, qty)
		except (csv.Error, UnicodeDecodeError) as exc:
			raise FeedFormatError(f"quantity file line {reader.line_num}: {exc}") from None


def iter_deleted(path):
	"""``rsrdeletedinv.txt`` → stock numbers."""
	for row in _tilde_rows(path, "deleted"):
		if row and (sku := (row[0] or "").strip()):
			yield sku


def _restriction_field(value) -> str:
	# this file quotes with '"', not '~' (the Data Service, measured on the production file)
	return str(value or "").strip().strip('"').strip()


def iter_restrictions(path, stats: dict | None = None):
	"""``rsr-ship-restrictions.txt`` → ``(sku, [municipality, state, type,
	detail])`` per line, in file order. At the end of the file, no data line
	or more than 1% of lines of the wrong shape raises FeedFormatError (the
	store's partial-file rule judges the count)."""
	stats = {} if stats is None else stats
	with open(path, newline="", encoding="latin-1") as fh:
		for line in fh:
			line = line.rstrip("\r\n")
			if not line:
				continue
			stats["total"] = stats.get("total", 0) + 1
			row = line.split(";")
			sku = _restriction_field(row[0]) if len(row) == RESTRICTION_COLUMN_COUNT else ""
			if not sku:
				stats["skipped"] = stats.get("skipped", 0) + 1
				continue
			yield sku, [_restriction_field(row[1]), _restriction_field(row[2]).upper()[:2],
				_restriction_field(row[3]).upper(), _restriction_field(row[4]).upper()]
	if not stats.get("total"):
		raise FeedFormatError("restrictions file has no data lines — refusing it")
	if _too_many(stats, "skipped"):
		raise FeedFormatError(f"restrictions layout mismatch: {stats['skipped']}/"
			f"{stats['total']} lines unparseable")


def file_content_hash(path) -> str:
	digest = hashlib.sha256()
	with open(path, "rb") as fh:
		for block in iter(lambda: fh.read(1 << 20), b""):
			digest.update(block)
	return digest.hexdigest()
