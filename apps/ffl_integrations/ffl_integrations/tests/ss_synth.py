"""Synthetic Sports South responses for the local-catalog tests. Every value is made up
here: item numbers ZZS-nnnnn, 12-digit UPCs under a 99992 prefix, and the brand
"Acme Test Arms". No distributor's real items, UPCs, titles or prices.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BRAND_NO = "9001"
BRAND = "Acme Test Arms"
NS = "http://webservices.theshootingwarehouse.com/smart/Inventory.asmx"

# (CATID, CATDES, DEPID, DEP, attribute labels by slot)
CATEGORIES = [
	("26", "TEST HANDGUNS", "1", "TEST FIREARMS", {"ATTR2": "Caliber"}),
	("40", "TEST RIFLES", "1", "TEST FIREARMS", {"ATTR2": "Caliber"}),
	("28", "TEST HOLSTERS", "2", "TEST ACCESSORIES", {}),
	("5", "TEST HANDGUN AMMO", "3", "TEST AMMUNITION", {"ATTR2": "Caliber"}),
	("31", "TEST LIGHTS", "2", "TEST ACCESSORIES", {}),
	("93", "TEST SUPPRESSORS", "1", "TEST FIREARMS", {}),
	("13", "TEST BLACK POWDER", "4", "TEST MUZZLELOADING", {"ATTR2": "Caliber"}),
	("8", "TEST BARRELS", "5", "TEST PARTS", {}),
]

# (CATID, ITYPE, title, caliber). Item n uses ROWS[(n - 1) % len(ROWS)]:
# n=1 is a handgun, 2 a holster, 3 ammo, 4 a rifle, 5 a light.
ROWS = (
	("26", "1", "ACME TEST PISTOL 9MM 15RD", "9MM"),
	("28", "3", "ACME TEST HOLSTER OWB BLACK", ""),
	("5", "3", "ACME TEST AMMO 9MM 115GR 50RD", "9MM"),
	("40", "2", "ACME TEST RIFLE 5.56 16IN", "5.56"),
	("31", "3", "ACME TEST WEAPON LIGHT 1000LM", ""),
)

HEADER = '<?xml version="1.0" encoding="utf-8"?>\n<string xmlns="' + NS + '">'


def sku(n: int) -> str:
	return f"ZZS-{n:05d}"


def upc(n: int) -> str:
	return f"99992{n:07d}"


def _esc(inner: str) -> str:
	return inner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def envelope(inner: str) -> str:
	return HEADER + _esc(inner) + "</string>"


def dataset(*records: str) -> str:
	return "<NewDataSet>" + "\n".join(records) + "</NewDataSet>"


def record(tag: str, fields: dict) -> str:
	"""Values are inner-XML text; a caller pre-escapes '&' as '&amp;'. None drops the tag."""
	body = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items() if v is not None)
	return f"<{tag}>{body}</{tag}>"


def item(n: int, **over) -> str:
	catid, itype, title, caliber = ROWS[(n - 1) % len(ROWS)]
	fields = {
		"ITEMNO": sku(n),
		"IDESC": "ACME TEST",
		"ITYPE": itype,
		"CATID": catid,
		"SHDESC": title,
		"PRC1": f"{150 + n % 400}.00",
		"CPRC": f"{100 + n % 400}.50",
		"QTYOH": str(n % 7),
		"WTPBX": "1.500",
		"ITUPC": upc(n),
		"MFGINO": f"ATS-{n}",
		"MFPRTYP": "M" if n % 3 == 0 else "",
		"MFPRC": f"{150 + n % 400}.00",
		"TXTREF": sku(n),
		"PICREF": sku(n),
		"ITBRDNO": BRAND_NO,
		"IMODEL": f"MODEL {n}",
		"ITATR2": caliber,
		"HAZAIR": "Y" if catid == "5" else "",
	}
	fields.update(over)
	return record("Table", fields)


def daily_items(numbers, over=None) -> str:
	return envelope(dataset(*(item(n, **(over or {}).get(n, {})) for n in numbers)))


def write_daily_items(path: str, count: int) -> int:
	"""Stream the same document as daily_items(range(1, count + 1)) to a file. Returns the size."""
	with open(path, "w", encoding="utf-8") as f:
		f.write(HEADER + _esc("<NewDataSet>"))
		for n in range(1, count + 1):
			f.write(_esc(item(n) + "\n"))
		f.write(_esc("</NewDataSet>") + "</string>")
	return os.path.getsize(path)


def categories_text() -> str:
	records = [
		record("Table", {"CATID": catid, "CATDES": catdes, "DEPID": depid, "DEP": dep, **attrs})
		for catid, catdes, depid, dep, attrs in CATEGORIES
	]
	return envelope(dataset(*records))


def brands_text() -> str:
	return envelope(dataset(record("Table", {"BRDNO": BRAND_NO, "BRDNM": BRAND})))


def servertime(minutes: int = 0) -> str:
	now = datetime.now(ZoneInfo("America/Chicago")) + timedelta(minutes=minutes)
	return now.strftime("%Y-%m-%dT%H:%M:%S") + ".000+00:00"


def _onhand_fields(sku_: str, qty, cost) -> dict:
	return {"I": sku_, "Q": str(qty), "P": "", "C": "" if cost is None else str(cost)}


def onhand_incremental(stamp: str, rows) -> str:
	if stamp == "" and not rows:
		return envelope("<NewDataSet />")
	parts = [f"<ServerDateTime><SERVERTIME>{stamp}</SERVERTIME></ServerDateTime>"]
	parts += [record("Onhand", _onhand_fields(s, q, c)) for s, q, c in rows]
	return envelope(dataset(*parts))


def onhand_full(rows) -> str:
	return envelope(dataset(*(record("Table", _onhand_fields(s, q, c)) for s, q, c in rows)))


def text_rows(pairs) -> str:
	return envelope(dataset(*(record("Table", {"ITEMNO": s, "TEXT": t}) for s, t in pairs)))


def error_payload(message: str, table1: bool = True) -> str:
	if table1:
		return envelope(dataset("<Table1><ERROR>" + message + "</ERROR></Table1>"))
	return envelope("<ERROR>" + message + "</ERROR>")


def standard_feed(numbers=range(1, 13)) -> dict:
	return {
		"CategoryUpdate": categories_text(),
		"BrandUpdate": brands_text(),
		"DailyItemUpdate": daily_items(numbers),
		"IncrementalOnhandUpdate": onhand_incremental(servertime(), []),
		"OnhandUpdate": onhand_full([(sku(n), n % 7, 100 + n % 400 + 0.5) for n in numbers]),
		"ListNewText": text_rows([(sku(n), f"Synthetic copy {n}.") for n in numbers]),
	}


class FakeSSTransport:
	"""Stands in for the driver's inventory transport. ``files`` maps a method name to the
	response text, or to a callable that writes the response to the path it is given."""

	def __init__(self, files=None):
		self.files = dict(files or {})
		self.fail = {}
		self.calls = []
		self.timeouts = {}
		self.left_behind = []

	def download(self, method, params=None, *, timeout=None):
		self.calls.append((method, dict(params or {})))
		self.timeouts[method] = timeout
		if method in self.fail:
			raise self.fail[method]
		fd, path = tempfile.mkstemp(prefix="ss-synth-", suffix="-" + method)
		self.left_behind.append(path)
		payload = self.files[method]
		if callable(payload):
			os.close(fd)
			payload(path)
		else:
			with os.fdopen(fd, "w", encoding="utf-8") as f:
				f.write(payload)
		return path
