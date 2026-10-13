"""Golden snapshot of what the two providers plan, place and price, per lane.

Written BEFORE the lane refactor and compared after it: the lane tables,
destinations and split rules move into ``distributor/lanes.py``, and every value
below must come out byte-identical. Pure — no site, no HTTP.

Three deliberate edits to the recorded file since, both from the review of that PR:

1. ``capabilities`` gained ``to_store`` — the lane a house needs to declare to run
   stocking / procure-to-store orders.
2. The four lane/goods combinations that were never safe to place — a firearm on
   ``direct`` (it would be addressed to the buyer's own door) and an accessory on
   ``fds`` (the house refuses it) — now read ``{"refused": …}`` instead of a plan.
3. The classification message names the catalog's firearm verdict (the service
   class) instead of the house's ``ffl_required`` flag (gun parts, 2026-09-26).

Anything else that changes here is a behaviour change, not a refactor.

Regenerate deliberately (only when a change to the output is intended):

    GOLDEN_UPDATE=1 bench --site dev.localhost run-tests \\
        --module ffl_integrations.tests.test_distributor_lane_golden
"""
from __future__ import annotations

import json
import os
import types
import unittest
from pathlib import Path

from ffl_integrations.distributor import rsr_provider as rsr
from ffl_integrations.distributor import ss_provider as ss
from ffl_integrations.distributor.base import PlanningError

GOLDEN = Path(__file__).parent / "golden" / "lane_plans.json"

_MODES = ("stocking", "to_store", "direct", "fds")

_RSR_GUN = {"sku": "RSR-GUN", "item_code": "RSR-GUN", "qty": 1, "department": "01",
	"is_firearm": True, "ffl_required": True, "unit_cost": 400.0}
_RSR_LONG = {"sku": "RSR-RIFLE", "item_code": "RSR-RIFLE", "qty": 1, "department": "05",
	"is_firearm": True, "ffl_required": True, "unit_cost": 700.0}
_RSR_ACC = {"sku": "RSR-ACC", "item_code": "RSR-ACC", "qty": 2, "department": "13",
	"is_firearm": False, "ffl_required": False, "unit_cost": 20.0}
_SS_GUN = {"sku": "SS-GUN", "item_code": "SS-GUN", "qty": 1, "department": "26",
	"is_firearm": True, "ffl_required": True, "unit_cost": 450.0}
_SS_LONG = {"sku": "SS-RIFLE", "item_code": "SS-RIFLE", "qty": 1, "department": "40",
	"is_firearm": True, "ffl_required": True, "unit_cost": 800.0}
_SS_AMMO = {"sku": "SS-AMMO", "item_code": "SS-AMMO", "qty": 3, "department": "4",
	"is_firearm": False, "ffl_required": False, "unit_cost": 15.0}

_CTX = {
	"ship_to": {"line1": "5 Elm St", "line2": "Apt 2", "city": "Springfield", "state": "TX", "zip": "75024"},
	"destination_ffl": {"license": "1-99-888-01-2D-09999", "premise_street": "9 Main",
		"premise_city": "Dallas", "premise_state": "TX", "premise_zip": "75201",
		"ffl_record": "ATF-REC-1"},
	"consumer_name": "Ann Buyer",
	"contact_num": "5551234567",
	"fill_or_kill": True,
}

_RSR_SETTINGS = types.SimpleNamespace(rate_ground=10.0, rate_2day=15.0, fee_signature=5.0,
	free_freight_threshold=500.0, store_free_freight=0)
_SS_SETTINGS = types.SimpleNamespace(
	rate_handgun_2day=10.95, rate_next_day=42.95, rate_long_gun_ground=10.95,
	rate_2day_air=27.95, rate_accessory_economy=7.95, rate_accessory_ground=10.95,
	fee_adult_signature=2.75, fee_signature=2.00, fee_insurance_min=2.90, fee_insurance_pct=0.95,
	accessory_economy=0, handgun_free_qty=3, handgun_free_value=750.0,
	store_ship_name="The Shop", store_ship_attn="", store_ship_line1="1 Range Rd",
	store_ship_line2="", store_ship_city="Springfield", store_ship_state="TX",
	store_ship_zip="75024", store_ship_phone="5559876543")


def _do(order_type, **kw):
	base = {"name": "DO-2609-00001", "order_type": order_type, "wish_qty": 1,
		"fill_or_kill": True, "consumer_name": "Ann Buyer", "contact_num": "5551234567",
		"transfer_ffl_license": "1-99-888-01-2D-09999", "transfer_ffl_record": "",
		"transfer_ffl_premise_street": "9 Main", "transfer_ffl_premise_city": "Dallas",
		"transfer_ffl_premise_state": "TX", "transfer_ffl_premise_zip": "75201",
		"ship_address_line1": "5 Elm St", "ship_address_line2": "Apt 2",
		"ship_address_city": "Springfield", "ship_address_state": "TX", "ship_address_zip": "75024",
		"company": "", "lines": [], "web_ref": ""}
	base.update(kw)
	return types.SimpleNamespace(**base)


def _plan_cases(provider, gun, long_gun, other, cap):
	"""4 lanes × firearm / accessory / mixed, plus one cart over the line cap."""
	out = {}
	carts = {
		"firearm": [gun],
		"accessory": [other],
		"mixed": [gun, other, long_gun],
		"over_cap": [{**other, "sku": f"{other['sku']}-{i}", "item_code": f"{other['sku']}-{i}"}
			for i in range(cap + 2)],
	}
	for mode in _MODES:
		for cart_name, lines in carts.items():
			try:
				plans = provider.plan_fulfillment(lines, {**_CTX, "mode": mode})
			except PlanningError as exc:
				out[f"{mode}/{cart_name}"] = {"refused": str(exc)}
				continue
			# The over-cap cart is 400+ identical lines; record its SHAPE (how the
			# split fell), not 400 copies of one line.
			out[f"{mode}/{cart_name}"] = [
				{"order_type": p["order_type"], "account": p["account"],
					"line_count": len(p["lines"]),
					"first_sku": p["lines"][0]["sku"], "last_sku": p["lines"][-1]["sku"]}
				for p in plans] if cart_name == "over_cap" else plans
	return out


def _classification_error(provider, line):
	try:
		provider.plan_fulfillment([{**line, "ffl_required": not line["ffl_required"]}],
			{"mode": "direct"})
	except PlanningError as exc:
		return str(exc)
	return "NO ERROR RAISED"


def _snapshot() -> dict:
	rsr_p, ss_p = rsr.RSRProvider(), ss.SSProvider()
	snap = {
		"rsr": {
			"capabilities": sorted(rsr_p.capabilities()),
			"plans": _plan_cases(rsr_p, _RSR_GUN, _RSR_LONG, _RSR_ACC, rsr.MAX_ORDER_SKUS),
			"classification_error": _classification_error(rsr_p, _RSR_GUN),
			"place_payload": {
				order_type: rsr.build_place_order_payload(
					_do(order_type, lines=[{"sku": "RSR-ACC", "wish_qty": 2, "ship_method": "Grnd"}]),
					order_email="orders@example.com")
				for order_type in ("Stocking", "Procure To Store", "Dropship Accessory",
					"Dropship Firearm")
			},
			"freight": {
				mode: rsr.estimate_freight([_RSR_GUN, _RSR_ACC], mode, _RSR_SETTINGS)
				for mode in _MODES
			},
		},
		"ss": {
			"capabilities": sorted(ss_p.capabilities()),
			"plans": _plan_cases(ss_p, _SS_GUN, _SS_LONG, _SS_AMMO, ss.MAX_ORDER_SKUS),
			"classification_error": _classification_error(ss_p, _SS_GUN),
			"ship_to": {
				mode: ss.build_ship_to(_do("Stocking"), mode, _SS_SETTINGS) for mode in _MODES
			},
			"freight": {
				"consumer": ss.estimate_freight(
					[{**_SS_GUN, "wish_qty": 1, "ship_method": "2Day"}], _SS_SETTINGS, True,
					adult_signature=True, signature=True, insurance=True),
				"store": ss.estimate_store_freight([_SS_GUN, _SS_AMMO], _SS_SETTINGS),
			},
		},
	}
	return json.loads(json.dumps(snap, sort_keys=True, default=str))


class LaneGoldenTest(unittest.TestCase):
	"""Every planned order, placement payload, ship-to block and freight figure the
	two providers produce today, lane by lane."""

	def test_matches_the_recorded_snapshot(self):
		current = _snapshot()
		if os.environ.get("GOLDEN_UPDATE"):
			GOLDEN.parent.mkdir(parents=True, exist_ok=True)
			GOLDEN.write_text(json.dumps(current, indent="\t", sort_keys=True) + "\n")
		recorded = json.loads(GOLDEN.read_text())
		for house in sorted(recorded):
			for key in sorted(recorded[house]):
				with self.subTest(house=house, key=key):
					self.assertEqual(current[house][key], recorded[house][key])
		self.assertEqual(sorted(current), sorted(recorded))


if __name__ == "__main__":
	unittest.main()
