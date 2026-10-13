"""Synthetic RSR feed files for the local-catalog tests, built from the parser's
own column map (``catalog_local/drivers/rsr_feed.py``). Every value is made up
here — stock numbers ``ZZR-nnnnnn``, 12-digit UPCs under a 99991 prefix, the
brand "Acme Test Arms" — no distributor's real SKUs, UPCs, titles or prices.

``measure()`` is the AC 6 check: a 200k-row snapshot through the real catalog
lane, with peak RSS and wall time (run it with ``bench execute``).
"""
from __future__ import annotations

import os
import shutil
import tempfile

from ffl_integrations.catalog_local.drivers import rsr_feed as F

BRAND = "Acme Test Arms"
TITLES = ("ACME TEST PISTOL 9MM 15RD", "ACME TEST HOLSTER OWB BLACK",
	"ACME TEST AMMO 9MM 115GR 50RD", "ACME TEST RIFLE 5.56 16IN", "ACME TEST WIDGET {n}")
# The RSR department each title is filed under (Handguns, Holsters/Pouches,
# Ammunition, Sporting Long Guns, Misc. Accessories).
DEPARTMENTS = ("01", "14", "18", "05", "13")
_COLUMNS = {
	"sku": F.COL_RSR_STOCK_NUMBER, "upc": F.COL_UPC, "title": F.COL_SHORT_DESCRIPTION,
	"department": F.COL_DEPARTMENT_NUMBER, "msrp": F.COL_MSRP, "cost": F.COL_DEALER_PRICE,
	"weight": F.COL_WEIGHT, "qty": F.COL_QUANTITY, "model": F.COL_MODEL,
	"manufacturer": F.COL_MANUFACTURER, "mfg_number": F.COL_MFG_NUMBER, "status": F.COL_STATUS,
	"long_description": F.COL_LONG_DESCRIPTION, "image_name": F.COL_IMAGE_NAME,
	"ground_only": F.COL_SHIPS_GROUND_ONLY, "signature": F.COL_SIGNATURE_REQUIRED,
	"dropship_blocked": F.COL_DROPSHIP_BLOCKED, "map": F.COL_MAP_PRICE, "prop65": F.COL_PROP65,
	"vendor_approval": F.COL_VENDOR_APPROVAL_REQUIRED,
}


def sku(n: int) -> str:
	return f"ZZR-{n:06d}"


def upc(n: int) -> str:
	return f"99991{n:07d}"


def catalog_fields(n: int, *, long_text: int = 40, **over) -> list:
	"""Row ``n`` as the 77 fields of RSR's inventory layout."""
	f = [""] * F.CATALOG_COLUMN_COUNT
	values = {"sku": sku(n), "upc": upc(n), "title": TITLES[n % len(TITLES)].format(n=n),
		"department": DEPARTMENTS[n % len(TITLES)], "msrp": f"{200 + n % 500}.00",
		"cost": f"{100 + n % 400}.50", "weight": "1.5", "qty": str(n % 7), "model": f"MODEL {n}",
		"manufacturer": BRAND, "mfg_number": f"ATA-{n}", "status": "",
		"long_description": (f"Synthetic product {n}. " * 50)[:long_text], "image_name": "",
		"map": f"{150 + n % 400}.00" if n % 3 == 0 else ""}
	values.update(over)
	for key, value in values.items():
		f[_COLUMNS[key]] = str(value)
	return f


def catalog_line(n: int, **over) -> str:
	"""One line, ended with RSR's trailing ';' (78 fields, the last empty)."""
	return ";".join(catalog_fields(n, **over)) + ";"


def catalog_text(numbers, over: dict | None = None, **common) -> str:
	"""Lines for ``numbers``; ``over`` = {n: {field: value}} per row."""
	over = over or {}
	return "".join(catalog_line(n, **{**common, **over.get(n, {})}) + "\n" for n in numbers)


def write_catalog(path: str, count: int, **common) -> int:
	"""Stream ``count`` rows to ``path`` (never held in memory); returns bytes."""
	with open(path, "w", encoding="latin-1", newline="") as fh:
		for n in range(1, count + 1):
			fh.write(catalog_line(n, **common) + "\n")
	return os.path.getsize(path)


def quantity_text(pairs) -> str:
	return "".join(f"{s},{q}\n" for s, q in pairs)


def deleted_text(skus) -> str:
	return "".join(f"{s};DELETED\n" for s in skus)


def restrictions_text(rows) -> str:
	"""``rows``: (sku, municipality, state, type, detail); RSR quotes this file with '"'."""
	return "".join(f'{s};"{m}";{st};{t};{d}\n' for s, m, st, t, d in rows)


class FakeTransport:
	"""Stands in for ``rsr.FtpsTransport``: each download writes a fresh temp
	copy of the file the test set (the lane deletes it afterwards)."""

	def __init__(self, files: dict | None = None):
		self.files = dict(files or {})  # kind -> text, or a callable(path) that writes it
		self.fail: dict = {}            # kind -> exception to raise
		self.calls: list = []
		self.left_behind: list = []

	def download(self, kind: str) -> str:
		self.calls.append(kind)
		if kind in self.fail:
			raise self.fail[kind]
		fd, path = tempfile.mkstemp(prefix="rsr-synth-", suffix="-" + kind)
		os.close(fd)
		payload = self.files[kind]
		if callable(payload):
			payload(path)
		else:
			with open(path, "w", encoding="latin-1", newline="") as fh:
				fh.write(payload)
		self.left_behind.append(path)
		return path


def measure(rows: int = 200_000, code: str = "rsrperf", restriction_lines: int = 500_000) -> dict:
	"""AC 6: a ``rows``-line synthetic snapshot through the catalog lane (download
	stand-in → streaming parser → load_catalog), then the unchanged reload and the
	quantity lane, then a ``restriction_lines``-line restrictions file, on this
	site. Reports peak RSS (this process, MiB), wall
	seconds per step and the file size; deletes its rows afterwards.

	bench --site <site> execute ffl_integrations.tests.rsr_synth.measure --kwargs "{'rows': 200000}"
	"""
	import resource
	import time

	import frappe

	from ffl_integrations.catalog_local import sync
	from ffl_integrations.catalog_local.drivers.rsr import RsrDriver

	def rss_mib():
		peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
		return round(peak / 1024 if os.uname().sysname == "Linux" else peak / (1 << 20), 1)

	transport = FakeTransport()
	driver = sync.register(RsrDriver(code=code, transport=transport))
	fd, src = tempfile.mkstemp(suffix="-perf-catalog")
	os.close(fd)
	out = {"rows": rows, "rss_start_mib": rss_mib()}
	try:
		out["file_mib"] = round(write_catalog(src, rows, long_text=600) / (1 << 20), 1)
		transport.files["catalog"] = lambda path: shutil.copyfile(src, path)
		transport.files["quantity"] = lambda path: _write_qty(path, rows)
		transport.files["deleted"] = deleted_text([])  # the catalog lane reads the deleted list first
		for step in ("load", "reload"):
			started = time.monotonic()
			result = sync.run_lane(code, "catalog", force=True)
			out[f"{step}_seconds"] = round(time.monotonic() - started, 1)
			out[f"{step}_result"] = {k: result.get(k) for k in ("created", "updated", "total")}
			out[f"rss_after_{step}_mib"] = rss_mib()
		started = time.monotonic()
		result = sync.run_lane(code, "quantity", force=True)
		out["quantity_seconds"] = round(time.monotonic() - started, 1)
		out["quantity_result"] = result
		# RSR's restrictions file runs ~0.5M lines over ~22k SKUs
		lines = restriction_lines
		transport.files["restrictions"] = lambda path: _write_restrictions(path, lines)
		started = time.monotonic()
		result = sync.run_lane(code, "restrictions", force=True)
		out["restrictions_seconds"] = round(time.monotonic() - started, 1)
		out["restrictions_result"] = result
		out["rss_peak_mib"] = rss_mib()
		out["job_timeout_seconds"] = driver.lane("catalog").timeout
	finally:
		os.unlink(src)
		sync.unregister(code)
		for table in ("dc_offer", "dc_product_move", "dc_restriction", "dc_meta"):
			frappe.db.sql(f"DELETE FROM {table} WHERE distributor = %s", (code,))
		frappe.db.sql("DELETE FROM dc_product WHERE upc LIKE %s", ("99991%",))
		frappe.db.sql("DELETE FROM dc_sync_run WHERE source LIKE %s", (code + "\\_%",))
		frappe.db.commit()
	print(out)
	return out


def _write_qty(path: str, rows: int) -> None:
	with open(path, "w", encoding="utf-8") as fh:
		for n in range(1, rows + 1):
			fh.write(f"{sku(n)},{(n + 1) % 7}\n")


def _write_restrictions(path: str, lines: int) -> None:
	states = ("NY", "NJ", "IL", "CA", "MA", "CT", "HI", "MD", "WA", "CO")
	with open(path, "w", encoding="latin-1") as fh:
		for i in range(lines):
			n = i // 24 + 1  # ~24 lines per SKU, like RSR's file
			fh.write(f'{sku(n)};"ALL";{states[i % len(states)]};S;F\n')
