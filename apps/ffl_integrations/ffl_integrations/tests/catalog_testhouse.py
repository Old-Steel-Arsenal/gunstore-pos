"""A synthetic distributor for the local catalog tests — house code ``testhouse``.

Two lanes over in-memory feeds the test fills: ``snapshot`` (heavy, a full
catalog load) and ``quantity``. Every value is made up here: no distributor's
real SKUs, UPCs, brands or prices.
"""
from __future__ import annotations

from datetime import timedelta

import frappe

from ffl_integrations.catalog_local import store, sync

CODE = "testhouse"
CODE2 = "testhouse2"


def make_row(n: int, **over) -> dict:
	"""Synthetic row ``n``: 12-digit UPCs under a made-up 99990 prefix."""
	row = {"sku": f"TH-{n:05d}", "upc": f"99990{n:07d}", "manufacturer": "Acme Arms",
		"model": f"Model {n}", "department": "01" if n % 2 == 0 else "20",
		"department_name": "Test Guns" if n % 2 == 0 else "Test Parts",
		"cost": 100.0 + n, "map": 150.0 + n if n % 3 == 0 else 0.0, "msrp": 0.0,
		"qty": n % 4, "ffl_required": n % 2 == 0, "dropship_blocked": False,
		"vendor_approval_required": False, "image_url": f"https://img.example.com/th/{n}.jpg",
		"discontinued": False, "status_flag": "", "short_description": f"ACME TEST WIDGET {n}",
		"mfg_number": f"AC-{n}", "caliber": "9mm" if n % 2 == 0 else "",
		"long_description": f"Synthetic long description {n}.", "image_name": f"{n}.jpg",
		"image_url_hires": "", "weight_lb": 1.5, "prop65": False, "ships_ground_only": False,
		"signature_required": False}
	row.update(over)
	return row


class TestHouseDriver(sync.Driver):
	label = "Test house"
	feed_stale = {"quantity": 5400, "snapshot": 21600}

	def __init__(self, code: str = CODE):
		self.code = code
		self.snapshot_rows: list = []
		self.quantity_pairs: list = []
		self.fail: set = set()     # lane names whose body raises
		self.gate_fail: set = set()  # lane names whose gate raises
		self.calls: list = []

	def configured(self) -> bool:
		return True

	def lanes(self):
		return (
			sync.Lane("snapshot", gate=lambda run: self._gate(run, "snapshot", timedelta(hours=2)),
				run=self._snapshot, heavy=True, timeout=sync.HEAVY_TIMEOUT_SECONDS),
			sync.Lane("quantity", gate=lambda run: self._gate(run, "quantity", timedelta(minutes=14)),
				run=self._quantity),
		)

	def _gate(self, run, lane, stale):
		if lane in self.gate_fail:
			raise RuntimeError(f"synthetic broken gate: {lane}")
		return run.due(lane, stale)

	def _snapshot(self, run):
		self.calls.append("snapshot")
		if "snapshot" in self.fail:
			raise sync.FeedUnavailable("synthetic outage")
		return store.load_catalog(iter(self.snapshot_rows), distributor=self.code)

	def _quantity(self, run):
		self.calls.append("quantity")
		if "quantity" in self.fail:
			raise sync.FeedFormatError("synthetic bad file")
		return store.apply_quantity(self.quantity_pairs, distributor=self.code)


def install(code: str = CODE) -> TestHouseDriver:
	return sync.register(TestHouseDriver(code))


def uninstall(*codes) -> None:
	for code in codes or (CODE, CODE2):
		sync.unregister(code)


def purge(*codes) -> None:
	"""Delete every dc_* row of the test houses, committed (the store commits, so
	the test rollback cannot clean these)."""
	codes = tuple(codes or (CODE, CODE2))
	for table in ("dc_offer", "dc_product_move", "dc_restriction", "dc_meta"):
		frappe.db.sql(f"DELETE FROM {table} WHERE distributor IN %(c)s", {"c": codes})
	frappe.db.sql("DELETE FROM dc_product WHERE upc LIKE %s", ("9999%",))
	for code in codes:
		frappe.db.sql("DELETE FROM dc_sync_run WHERE source LIKE %s", (code.replace("_", "\\_") + "\\_%",))
	frappe.db.commit()
