"""Web drop-ship router: lane classification, the two routing gates
(paid + FFL captured), ship-context assembly, and the WooCommerce
tracking-note format.

Everything here is PURE (no DB, no HTTP) so it runs in the harness as well as
under `make test`. The frappe-backed half of the router (real Woo Online Order
reads, DO creation, the doc_event / cron triggers) lives in
`test_web_dropship_complete.py`.

The tracking-note case is a CONTRACT test against WordPress: the note this app
writes is parsed by the storefront plugin's `class-order-tracking.php`, so the regex below is
a verbatim transcription of the one in `includes/class-order-tracking.php`.
If that file changes, this test must be re-derived from it — not the other way
round.
"""
from __future__ import annotations

import re
import unittest

import frappe

from ffl_integrations.distributor import router
from ffl_integrations.distributor import tasks


# Verbatim from the storefront plugin's includes/class-order-tracking.php —
#   '/shipped via\s+(.+?)\s+on\s+(.+?)\s+with tracking number\s+(\S+?)(?:\s*\(Shipstation\))?\.?\s*$/im'
WP_TRACKING_RE = re.compile(
	r"shipped via\s+(.+?)\s+on\s+(.+?)\s+with tracking number\s+(\S+?)"
	r"(?:\s*\(Shipstation\))?\.?\s*$",
	re.IGNORECASE | re.MULTILINE,
)


# ----------------------------------------------------------------- lane matrix

class TestLaneClassification(unittest.TestCase):
	"""One order line → one fulfilment lane (Distributor Order Line
	`fulfillment`)."""

	def test_no_distributor_is_local(self):
		self.assertEqual(
			router.classify_lane(dropship_distributor=None, is_firearm=True),
			router.LANE_LOCAL)
		self.assertEqual(
			router.classify_lane(dropship_distributor="", is_firearm=False),
			router.LANE_LOCAL)

	def test_direct_firearm_is_fds(self):
		"""A drop-shipped firearm can never go to the buyer's door — it ships to
		their transfer FFL, which is RSR's FDS lane."""
		self.assertEqual(
			router.classify_lane(dropship_distributor="RSR", is_firearm=True,
				fulfillment_mode="Direct"),
			router.LANE_FDS)

	def test_direct_accessory_is_direct(self):
		self.assertEqual(
			router.classify_lane(dropship_distributor="RSR", is_firearm=False,
				fulfillment_mode="Direct"),
			router.LANE_DIRECT)

	def test_to_store_mode_wins_for_both_kinds(self):
		"""A blocked brand flips MODE, and both a gun and an accessory then
		come to the shop first."""
		self.assertEqual(
			router.classify_lane(dropship_distributor="RSR", is_firearm=True,
				fulfillment_mode="To Store"),
			router.LANE_TO_STORE)
		self.assertEqual(
			router.classify_lane(dropship_distributor="RSR", is_firearm=False,
				fulfillment_mode="To Store"),
			router.LANE_TO_STORE)

	def test_unknown_mode_falls_back_to_direct_semantics(self):
		"""A listing row with a blank fulfillment_mode must not silently become
		local — it IS a drop-ship item; Direct is the documented default."""
		self.assertEqual(
			router.classify_lane(dropship_distributor="RSR", is_firearm=False,
				fulfillment_mode=""),
			router.LANE_DIRECT)

	def test_local_is_not_a_dropship_lane(self):
		self.assertFalse(router.is_dropship_lane(router.LANE_LOCAL))
		for lane in (router.LANE_DIRECT, router.LANE_FDS, router.LANE_TO_STORE):
			self.assertTrue(router.is_dropship_lane(lane), lane)


# ----------------------------------------------------------------- route gates

class TestRouteVerdict(unittest.TestCase):
	"""The two gates in front of DO creation: paid, and FFL captured."""

	def test_order_with_no_dropship_line_is_not_routed(self):
		v = router.route_verdict(woo_status="processing", lanes=["local", "local"],
			ffl_captured=True)
		self.assertFalse(v["route"])
		self.assertEqual(v["reason"], router.REASON_NO_DROPSHIP)

	def test_paid_dropship_order_routes(self):
		for status in ("processing", "completed"):
			v = router.route_verdict(woo_status=status, lanes=["direct"], ffl_captured=False)
			self.assertTrue(v["route"], status)
			self.assertEqual(v["reason"], "")

	def test_unpaid_order_is_held(self):
		"""An on-hold / pending / failed order must never reach RSR — we would be
		ordering stock against money we do not have."""
		for status in ("on-hold", "pending", "failed", "checkout-draft", ""):
			v = router.route_verdict(woo_status=status, lanes=["direct"], ffl_captured=True)
			self.assertFalse(v["route"], status)
			self.assertEqual(v["reason"], router.REASON_UNPAID, status)

	def test_h6_fds_without_captured_ffl_is_held(self):
		"""An FDS order needs the buyer's transfer FFL BEFORE placement —
		RSR ships the gun to that licence, and there is no cancel API."""
		v = router.route_verdict(woo_status="processing", lanes=["fds"], ffl_captured=False)
		self.assertFalse(v["route"])
		self.assertEqual(v["reason"], router.REASON_FFL_MISSING)

	def test_h6_gate_only_applies_to_the_fds_lane(self):
		"""An accessory-only drop-ship order has no FFL to capture."""
		v = router.route_verdict(woo_status="processing", lanes=["direct", "local"],
			ffl_captured=False)
		self.assertTrue(v["route"])

	def test_to_store_firearm_does_not_need_the_ffl_yet(self):
		"""To-Store guns are received into our own book first and dispositioned
		locally later; the FFL is needed at THAT point, not at order time."""
		v = router.route_verdict(woo_status="processing", lanes=["to_store"],
			ffl_captured=False)
		self.assertTrue(v["route"])

	def test_unpaid_is_reported_before_missing_ffl(self):
		"""Reason ordering is deliberate: an unpaid order is transient and must
		not raise the missing-FFL operations alert."""
		v = router.route_verdict(woo_status="on-hold", lanes=["fds"], ffl_captured=False)
		self.assertEqual(v["reason"], router.REASON_UNPAID)


# ------------------------------------------------------------- ship context

class TestShipContext(unittest.TestCase):
	"""What `plan_fulfillment` receives per lane group (ship_context)."""

	SHIP_TO = {"line1": "5 Elm St", "line2": "", "city": "Springfield", "state": "TX", "zip": "75024"}
	FFL = {"license": "1-57-XXX", "premise_street": "9 Main", "premise_city": "Austin",
		"premise_state": "TX", "premise_zip": "78701", "ffl_record": "ATF-1"}

	def test_direct_context_carries_the_buyers_address(self):
		ctx = router.build_ship_context(router.LANE_DIRECT, ship_to=self.SHIP_TO,
			destination_ffl=self.FFL, consumer_name="Ann Buyer", contact_num="555")
		self.assertEqual(ctx["mode"], "direct")
		self.assertEqual(ctx["ship_to"], self.SHIP_TO)
		self.assertIsNone(ctx["destination_ffl"])
		self.assertEqual(ctx["consumer_name"], "Ann Buyer")

	def test_fds_context_carries_the_ffl_not_the_home_address(self):
		"""A gun ships to the licence, and the buyer's street
		address must not leak onto an FDS order."""
		ctx = router.build_ship_context(router.LANE_FDS, ship_to=self.SHIP_TO,
			destination_ffl=self.FFL, consumer_name="Ann Buyer", contact_num="555")
		self.assertEqual(ctx["mode"], "fds")
		self.assertIsNone(ctx["ship_to"])
		self.assertEqual(ctx["destination_ffl"], self.FFL)

	def test_to_store_context_carries_neither(self):
		"""Procure-To-Store ships to US on the dealer account — the shop address
		is the account default, not per-order data."""
		ctx = router.build_ship_context(router.LANE_TO_STORE, ship_to=self.SHIP_TO,
			destination_ffl=self.FFL)
		self.assertEqual(ctx["mode"], "to_store")
		self.assertIsNone(ctx["ship_to"])
		self.assertIsNone(ctx["destination_ffl"])


# --------------------------------------------------- WooCommerce tracking note

class TestTrackingNoteFormat(unittest.TestCase):
	"""CONTRACT: the storefront plugin's order-tracking board parses these notes.

	Two properties matter and neither is obvious:

	1. The tracking number must end its LINE (`$` under /m, and `.` does not
	   cross newlines without /s), so the whole sentence lives on one line.
	2. WordPress uses `preg_match`, NOT `preg_match_all` — it captures ONE
	   shipment per note. Several parcels therefore need several notes.
	"""

	def test_note_matches_the_wordpress_parser(self):
		note = tasks.format_tracking_note("UPS", "2026-07-23", "1Z999AA10123456784")
		m = WP_TRACKING_RE.search(note)
		self.assertIsNotNone(m, f"WP parser would not match: {note!r}")
		self.assertEqual(m.group(1), "UPS")
		self.assertEqual(m.group(2), "2026-07-23")
		self.assertEqual(m.group(3), "1Z999AA10123456784")

	def test_note_carries_the_distributor_order_reference(self):
		note = tasks.format_tracking_note("UPS", "2026-07-23", "1Z999", do_name="DO-2607-00001")
		self.assertIn("DO-2607-00001", note)
		m = WP_TRACKING_RE.search(note)
		self.assertIsNotNone(m)
		# The reference must not be mistaken for the carrier or the date.
		self.assertEqual(m.group(1), "UPS")
		self.assertEqual(m.group(3), "1Z999")

	def test_blank_carrier_still_parses(self):
		"""RSR does not always name a carrier. An empty one would collapse
		`shipped via  on` and the WP regex would not match at all — so a
		placeholder is substituted rather than an empty string."""
		note = tasks.format_tracking_note("", "2026-07-23", "1Z999")
		m = WP_TRACKING_RE.search(note)
		self.assertIsNotNone(m, f"WP parser would not match: {note!r}")
		self.assertTrue(m.group(1).strip())

	def test_blank_date_still_parses(self):
		"""m1: the date capture ``(.+?)`` also requires ≥1 char — an empty ship_date
		would collapse `on  with` and the WP regex would not match. Asymmetric with
		the carrier fallback until this was fixed."""
		note = tasks.format_tracking_note("UPS", "", "1Z999")
		m = WP_TRACKING_RE.search(note)
		self.assertIsNotNone(m, f"WP parser would not match: {note!r}")
		self.assertTrue(m.group(2).strip())

	def test_tracking_number_ends_the_line(self):
		note = tasks.format_tracking_note("FedEx", "2026-07-23", "7712345678")
		line = [l for l in note.splitlines() if "tracking number" in l][0]
		self.assertTrue(line.rstrip().endswith("7712345678"))

	def test_one_note_per_parcel(self):
		"""`preg_match` stops at the first hit, so two trackings in one note
		would silently lose the second parcel."""
		notes = tasks.build_tracking_notes(
			[{"carrier": "UPS", "date": "2026-07-23", "number": "1Z1"},
			 {"carrier": "UPS", "date": "2026-07-24", "number": "1Z2"}],
			do_name="DO-2607-00001")
		self.assertEqual(len(notes), 2)
		found = [WP_TRACKING_RE.search(n).group(3) for n in notes]
		self.assertEqual(found, ["1Z1", "1Z2"])

	def test_duplicate_tracking_numbers_are_collapsed(self):
		"""Every poll re-reports the same TrackingNum; re-posting it would grow
		the order-notes list without bound."""
		notes = tasks.build_tracking_notes(
			[{"carrier": "UPS", "date": "2026-07-23", "number": "1Z1"},
			 {"carrier": "UPS", "date": "2026-07-23", "number": "1Z1"}],
			do_name="DO-2607-00001")
		self.assertEqual(len(notes), 1)

	def test_no_tracking_yields_no_notes(self):
		self.assertEqual(tasks.build_tracking_notes([], do_name="DO-1"), [])


# --------------------------------------------------- shipment extraction

class TestShipmentsFromLines(unittest.TestCase):
	"""DO lines carry CSV echoes from RSR (`tracking_numbers`); the completion
	path turns them into parcels."""

	def test_csv_tracking_is_split_and_deduped_across_lines(self):
		lines = [
			{"tracking_numbers": "1Z1,1Z2", "ship_method": "Grnd"},
			{"tracking_numbers": "1Z2", "ship_method": "Grnd"},
		]
		parcels = tasks.shipments_from_lines(lines, ship_date="2026-07-23")
		self.assertEqual([p["number"] for p in parcels], ["1Z1", "1Z2"])
		self.assertTrue(all(p["date"] == "2026-07-23" for p in parcels))

	def test_blank_entries_are_dropped(self):
		parcels = tasks.shipments_from_lines(
			[{"tracking_numbers": " ,, "}], ship_date="2026-07-23")
		self.assertEqual(parcels, [])



# ------------------------------------------------- provider ship blocks

class TestPlanFulfillmentShipBlocks(unittest.TestCase):
	"""`ship_to` / `ship_ffl` / `transfer_dealer` used to be left as None with a note that
	they would be populated per mode. This is that contract."""

	FFL = {"license": "1-99-888-01-2D-09999", "premise_street": "9 Main",
		"premise_city": "Austin", "premise_state": "TX", "premise_zip": "78701",
		"ffl_record": "ATF-1"}
	SHIP_TO = {"line1": "5 Elm St", "line2": "", "city": "Springfield", "state": "TX",
		"zip": "75024"}

	def _plan(self, ctx, is_firearm=False):
		from ffl_integrations.distributor.rsr_provider import RSRProvider

		line = {"sku": "X", "item_code": "X", "qty": 1, "ffl_required": is_firearm,
			"is_firearm": is_firearm, "department": "01" if is_firearm else "10"}
		return RSRProvider().plan_fulfillment([line], ctx)[0]

	def test_stocking_plan_carries_no_destination(self):
		"""REGRESSION: Flow A is unchanged."""
		plan = self._plan({"mode": "stocking"})
		self.assertIsNone(plan["ship_to"])
		self.assertIsNone(plan["ship_ffl"])
		self.assertIsNone(plan["transfer_dealer"])

	def test_direct_plan_carries_the_buyer_address_only(self):
		plan = self._plan({"mode": "direct", "ship_to": self.SHIP_TO,
			"consumer_name": "Ann Buyer", "contact_num": "555"})
		self.assertEqual(plan["ship_to"], self.SHIP_TO)
		self.assertIsNone(plan["ship_ffl"])
		self.assertEqual(plan["consumer_name"], "Ann Buyer")
		self.assertEqual(plan["contact_num"], "555")

	def test_fds_plan_carries_the_licence_not_the_address(self):
		plan = self._plan({"mode": "fds", "destination_ffl": self.FFL,
			"ship_to": self.SHIP_TO, "consumer_name": "Ann Buyer"}, is_firearm=True)
		self.assertEqual(plan["ship_ffl"], self.FFL["license"])
		self.assertEqual(plan["transfer_dealer"]["license"], self.FFL["license"])
		self.assertIsNone(plan["ship_to"],
			"a firearm order must not carry the buyer's home address")

	def test_to_store_plan_carries_neither(self):
		plan = self._plan({"mode": "to_store"}, is_firearm=True)
		self.assertIsNone(plan["ship_to"])
		self.assertIsNone(plan["ship_ffl"])
		self.assertEqual(plan["order_type"], "Procure To Store")
		self.assertEqual(plan["account"], "dealer")

	def test_fds_uses_the_fulfillment_account(self):
		plan = self._plan({"mode": "fds", "destination_ffl": self.FFL}, is_firearm=True)
		self.assertEqual(plan["account"], "fulfillment")
		self.assertEqual(plan["order_type"], "Dropship Firearm")


# =============================================== router planning core (m8: moved
# here from the mis-named test_distributor_accounting.py — these exercise
# plan_route / groups_to_create / rollup_status, all router functions)

PLAN_SHIP_TO = {"line1": "5 Elm St", "line2": "", "city": "Springfield", "state": "TX", "zip": "75024"}
PLAN_FFL = {"license": "1-99-888-01-2D-09999", "premise_street": "9 Main",
	"premise_city": "Austin", "premise_state": "TX", "premise_zip": "78701",
	"ffl_record": "ATF-1"}


# A distinct sentinel: `None` is a MEANINGFUL argument here ("this order has no
# FFL / no address"), so it cannot double as "use the default".
_UNSET = object()


def _plan_snap(lines, *, woo_status="processing", ship_to=_UNSET, ffl=_UNSET,
		consumer_name="Ann Buyer", contact_num="555-0100"):
	return {
		"name": "WOO-ORD-1",
		"doctype": "Woo Online Order",
		"woo_status": woo_status,
		"ship_to": PLAN_SHIP_TO if ship_to is _UNSET else ship_to,
		"destination_ffl": PLAN_FFL if ffl is _UNSET else ffl,
		"consumer_name": consumer_name,
		"contact_num": contact_num,
		"lines": lines,
	}


def _plan_ln(sku, lane, qty=1, item_code=None, distributor="RSR"):
	return {"sku": sku, "item_code": item_code or sku, "qty": qty, "lane": lane,
		"distributor": distributor if lane != "local" else None}


# ================================================================== planning

class TestPlanRoute(unittest.TestCase):

	def test_local_only_order_plans_nothing(self):
		plan = router.plan_route(_plan_snap([_plan_ln("GLOCK-19", "local")]))
		self.assertFalse(plan["route"])
		self.assertEqual(plan["groups"], [])
		self.assertEqual(plan["reason"], router.REASON_NO_DROPSHIP)

	def test_one_group_per_lane(self):
		"""Firearms and accessories never share an order; that is enforced downstream by plan_fulfillment, but the LANES must
		already be separate orders: they use different RSR accounts and different
		destinations."""
		plan = router.plan_route(_plan_snap([
			_plan_ln("RSR-GLK19", "fds"),
			_plan_ln("RSR-HOLO", "direct"),
			_plan_ln("RSR-GLOCK-B", "to_store"),
			_plan_ln("GLOCK-19", "local"),
		]))
		self.assertTrue(plan["route"])
		self.assertEqual([g["lane"] for g in plan["groups"]], ["fds", "to_store", "direct"])
		self.assertTrue(all(g["lines"] for g in plan["groups"]))

	def test_local_lines_never_enter_a_group(self):
		plan = router.plan_route(_plan_snap([
			_plan_ln("RSR-HOLO", "direct"), _plan_ln("GLOCK-19", "local")]))
		skus = [l["sku"] for g in plan["groups"] for l in g["lines"]]
		self.assertEqual(skus, ["RSR-HOLO"])

	def test_lines_of_one_lane_stay_together(self):
		plan = router.plan_route(_plan_snap([
			_plan_ln("A", "direct"), _plan_ln("B", "direct")]))
		self.assertEqual(len(plan["groups"]), 1)
		self.assertEqual([l["sku"] for l in plan["groups"][0]["lines"]], ["A", "B"])

	def test_group_carries_its_own_ship_context(self):
		plan = router.plan_route(_plan_snap([_plan_ln("RSR-GLK19", "fds"), _plan_ln("RSR-HOLO", "direct")]))
		by_lane = {g["lane"]: g for g in plan["groups"]}
		self.assertEqual(by_lane["fds"]["ship_context"]["destination_ffl"], PLAN_FFL)
		self.assertIsNone(by_lane["fds"]["ship_context"]["ship_to"])
		self.assertEqual(by_lane["direct"]["ship_context"]["ship_to"], PLAN_SHIP_TO)
		self.assertIsNone(by_lane["direct"]["ship_context"]["destination_ffl"])

	def test_unpaid_order_plans_no_groups(self):
		plan = router.plan_route(_plan_snap([_plan_ln("RSR-HOLO", "direct")], woo_status="on-hold"))
		self.assertFalse(plan["route"])
		self.assertEqual(plan["groups"], [])
		self.assertEqual(plan["reason"], router.REASON_UNPAID)

	def test_h6_fds_without_ffl_plans_nothing_at_all(self):
		"""Not even the accessory leg: holding the whole order is what makes the
		operator fix the FFL, and splitting a shipment the buyer may cancel is
		worse than waiting (RSR has no cancel API)."""
		plan = router.plan_route(_plan_snap(
			[_plan_ln("RSR-GLK19", "fds"), _plan_ln("RSR-HOLO", "direct")], ffl=None))
		self.assertFalse(plan["route"])
		self.assertEqual(plan["groups"], [])
		self.assertEqual(plan["reason"], router.REASON_FFL_MISSING)

	def test_direct_lane_without_an_address_is_held(self):
		"""A partial destination is not shippable; half an address is worse than
		no order."""
		plan = router.plan_route(_plan_snap([_plan_ln("RSR-HOLO", "direct")], ship_to=None))
		self.assertFalse(plan["route"])
		self.assertEqual(plan["reason"], router.REASON_SHIP_TO_MISSING)

	def test_fds_lane_needs_no_buyer_address(self):
		plan = router.plan_route(_plan_snap([_plan_ln("RSR-GLK19", "fds")], ship_to=None))
		self.assertTrue(plan["route"])

	def test_to_store_lane_needs_neither(self):
		plan = router.plan_route(_plan_snap([_plan_ln("RSR-GLK19", "to_store")],
			ship_to=None, ffl=None))
		self.assertTrue(plan["route"])

	def test_mixed_distributors_split_further(self):
		"""Two distributors cannot share one order — the DO names exactly one."""
		plan = router.plan_route(_plan_snap([
			_plan_ln("A", "direct", distributor="RSR"),
			_plan_ln("B", "direct", distributor="LIPSEYS")]))
		self.assertEqual(len(plan["groups"]), 2)
		self.assertEqual({g["distributor"] for g in plan["groups"]}, {"RSR", "LIPSEYS"})

	def test_group_order_is_deterministic(self):
		"""Same cart, same plan — a shuffling plan makes idempotency untestable."""
		lines = [_plan_ln("B", "direct"), _plan_ln("A", "fds"), _plan_ln("C", "to_store")]
		a = router.plan_route(_plan_snap(list(lines)))
		b = router.plan_route(_plan_snap(list(reversed(lines))))
		self.assertEqual([g["lane"] for g in a["groups"]],
			[g["lane"] for g in b["groups"]])


# ===================================================== idempotency of routing

class TestRoutingIsIdempotent(unittest.TestCase):
	"""Both triggers (doc_event and the cron sweep) can fire for one order, and a
	webhook redelivery can fire either again. Ordering twice from RSR is
	unrecoverable — there is no cancel API."""

	def test_a_group_already_ordered_is_skipped(self):
		existing = [{"lane": "fds", "distributor": "RSR", "status": "Placed"}]
		todo = router.groups_to_create(
			[{"lane": "fds", "distributor": "RSR"}, {"lane": "direct", "distributor": "RSR"}],
			existing)
		self.assertEqual([g["lane"] for g in todo], ["direct"])

	def test_a_draft_group_is_also_skipped(self):
		"""A Draft DO is already in the human queue; a second one would be ordered
		twice the moment both were confirmed."""
		existing = [{"lane": "direct", "distributor": "RSR", "status": "Draft"}]
		todo = router.groups_to_create([{"lane": "direct", "distributor": "RSR"}], existing)
		self.assertEqual(todo, [])

	def test_a_cancelled_group_may_be_recreated(self):
		"""The operator withdrew it; a re-route is a deliberate retry."""
		existing = [{"lane": "direct", "distributor": "RSR", "status": "Cancelled"}]
		todo = router.groups_to_create([{"lane": "direct", "distributor": "RSR"}], existing)
		self.assertEqual([g["lane"] for g in todo], ["direct"])

	def test_same_lane_different_distributor_is_a_different_group(self):
		existing = [{"lane": "direct", "distributor": "RSR", "status": "Placed"}]
		todo = router.groups_to_create(
			[{"lane": "direct", "distributor": "LIPSEYS"}], existing)
		self.assertEqual(len(todo), 1)


# ========================================================= dropship_status roll-up

class TestDropshipStatusRollup(unittest.TestCase):
	"""One order-level status summarising N distributor orders (order_status
	keeps its three values)."""

	def test_no_orders_yet_is_pending_route(self):
		self.assertEqual(router.rollup_status([]), router.DS_PENDING_ROUTE)

	def test_any_live_order_is_ordered(self):
		self.assertEqual(router.rollup_status(["Draft"]), router.DS_ORDERED)
		self.assertEqual(router.rollup_status(["Complete", "Placed"]), router.DS_ORDERED)

	def test_all_settled_is_complete(self):
		self.assertEqual(router.rollup_status(["Complete"]), router.DS_COMPLETE)
		self.assertEqual(router.rollup_status(["Complete", "Cancelled"]),
			router.DS_COMPLETE)

	def test_an_errored_order_surfaces_as_error(self):
		self.assertEqual(router.rollup_status(["Error", "Complete"]), router.DS_ERROR)

	def test_all_cancelled_is_not_a_completion(self):
		"""Nothing shipped — this must not read as done, or the completion path
		would invoice a dead order."""
		self.assertEqual(router.rollup_status(["Cancelled"]), router.DS_ERROR)


if __name__ == "__main__":
	unittest.main()
