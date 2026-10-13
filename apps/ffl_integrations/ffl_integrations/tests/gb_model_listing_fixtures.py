"""Shared scaffolding for the model-LISTING tests (the quantity, trigger, price and
sweep files): a real uniform-price gun Item with real guns on the shelf and real
GunBroker orders (the same documents `gb_model_fixtures.ModelUnitCase` builds), and
a fake GunBroker that numbers its listings and remembers what it was told.

What is faked is the network and nothing else. The pool (`uniform_sellable_units`),
the orders that give `ingested`, the claim rows and the Item the formula reads are
all real, because the formula is about how those four fit together and a mock of
any of them would test the mock.
"""
from __future__ import annotations

from unittest.mock import patch

import frappe

from ffl_integrations.gunbroker import item_listings, listings, mapping
from ffl_integrations.gunbroker.client import GunBrokerError
from ffl_integrations.tests import gunbroker_fixtures as fx
from ffl_integrations.tests.gb_model_fixtures import GB_ITEM, MODEL, ModelUnitCase

# The listing the order fixtures sell from (`GB_ITEM`), and what GunBroker moves it to
# when a partial sale ends it and relists the remainder.
LISTED = str(GB_ITEM)
REPLACEMENT = "16087400"

PISTOL_ROW = {"item_group": "", "firearm_type": "Pistol", "category_id": 3026,
	"category_name": "Semi Auto Pistols", "is_cr_eligible": 0, "is_antique": 0}


class FakeGB:
	"""A GunBroker with numbered listings: just enough client for the quantity,
	reprice and end paths.

	``listings`` maps an item number to ``{"quantity", "active", "relisted_as",
	"price"}``. A PUT to an inactive listing is accepted and discarded, as the real
	API does; ``errors`` makes a number unreadable; ``on_put`` runs before a PUT
	lands, to simulate a sale arriving between the read and the write.
	"""

	def __init__(self, **listed):
		self.listings = {str(k): {"quantity": 0, "active": True, "relisted_as": 0,
			"price": 499.0, **v} for k, v in listed.items()}
		self.errors: set[str] = set()
		self.puts: list[tuple[str, dict]] = []
		self.deletes: list[str] = []
		self.on_put = None

	def _detail(self, item_id: str) -> dict:
		row = self.listings[str(item_id)]
		return fx.item_detail(
			item_id, is_active=row["active"], fixed_price=row["price"],
			sku=MODEL, serial="", quantity=row["quantity"], relisted_as=row["relisted_as"])

	def item_probe(self, item_id) -> dict:
		item_id = str(item_id)
		if item_id in self.errors:
			return {"state": "error", "item": None}
		if item_id not in self.listings:
			return {"state": "gone", "item": None}
		return {"state": "ok", "item": self._detail(item_id)}

	def get_item(self, item_id) -> dict:
		if str(item_id) in self.errors or str(item_id) not in self.listings:
			raise GunBrokerError("no such item", status_code=404)
		return self._detail(item_id)

	def update_listing(self, item_id, patch_: dict):
		self.puts.append((str(item_id), dict(patch_)))
		if self.on_put:
			self.on_put(self)
		row = self.listings.get(str(item_id))
		if row and row["active"]:
			if "Quantity" in patch_:
				row["quantity"] = patch_["Quantity"]
			if "FixedPrice" in patch_:
				row["price"] = patch_["FixedPrice"]
		return {"userMessage": "ok"}

	def delete_listing(self, item_id):
		self.deletes.append(str(item_id))
		if str(item_id) in self.listings:
			self.listings[str(item_id)]["active"] = False
		return {"userMessage": "ended"}

	def sold(self, item_id, units: int, *, relist_as: str | None = None) -> None:
		"""GunBroker sells ``units`` from a listing: its count drops, and (as a partial
		sale does) it ends and the remainder lives on under ``relist_as``."""
		row = self.listings[str(item_id)]
		row["quantity"] -= units
		if relist_as:
			row["active"] = False
			row["relisted_as"] = int(relist_as)
			self.listings[str(relist_as)] = {"quantity": row["quantity"], "active": True,
				"relisted_as": 0, "price": row["price"]}
			row["quantity"] = 0


class ModelListingCase(ModelUnitCase):
	"""A listed (or listable) uniform-price gun, with everything outbound captured."""

	def setUp(self):
		super().setUp()
		self.enqueued: list[tuple[str, dict]] = []
		self.settings = fx.settings_stub(category_map=[frappe._dict(PISTOL_ROW)])
		self._start(patch.object(frappe, "enqueue",
			side_effect=lambda method, **kw: self.enqueued.append((method, kw))))
		self._start(patch.object(listings, "_settings", return_value=self.settings))
		self._start(patch.object(mapping, "listing_flags", return_value=fx.flags_stub()))
		self._start(patch.object(item_listings, "safe_log_error"))
		self._start(patch.object(listings, "notify_admins",
			side_effect=lambda s, m, **kw: self.alerts.append(kw["dedup_key"])))
		self.gb = FakeGB()
		frappe.db.set_value("Item", MODEL, {
			"item_name": "CZ 75 Grade A", "firearm_type": "Pistol", "standard_rate": 499,
			# A test that edits the Item as a person would (or turns it into something
			# else) must not hand the next one a different kind of Item.
			"is_firearm": 1, "uniform_sell_price": 1, "has_serial_no": 1,
			"sale_price": 0, "sale_from": None, "sale_to": None, "disabled": 0,
			"gb_item_id": "", "gb_item_history": "", "gb_delisted": 0, "gb_end_pending": 0,
			"gb_push_pending": 0, "gb_qty_pushed": 0, "gb_qty_basis": 0,
			"gb_qty_basis_id": "", "gb_price_pushed": 0, "gb_price_dirty": 0,
			"gb_synced_at": None, "gb_sync_error": "", "gb_featured": 0,
			"gb_showcase": 0, "gb_sponsored_onsite": 0, "gb_view_counter": 0,
		}, update_modified=False)

	def enqueued_methods(self) -> list[str]:
		return [m.rsplit(".", 1)[-1] for m, _kw in self.enqueued]

	def stock_n(self, n: int, tag: str = "M") -> list[str]:
		return [self.stock(f"{tag}{i}") for i in range(n)]

	def listed(self, *, basis: int, listing_id: str = LISTED, history: str = "",
			price: float = 0) -> None:
		"""The Item as a push of ``basis`` guns to ``listing_id`` leaves it."""
		frappe.db.set_value("Item", MODEL, {
			"gb_item_id": listing_id, "gb_item_history": history,
			"gb_qty_pushed": basis, "gb_qty_basis": basis, "gb_qty_basis_id": listing_id,
			"gb_qty_basis_ingested": 0,
			"gb_price_pushed": price or 499, "gb_synced_at": frappe.utils.now_datetime(),
		}, update_modified=False)

	def item(self) -> frappe._dict:
		return frappe._dict(item_listings.item_state(MODEL))

	def sync(self, **kw) -> dict:
		return item_listings.sync_model_quantity(MODEL, client=self.gb, **kw)
