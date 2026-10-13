"""NFA firearm_type vocabulary — the receive / trade-in classification chain.

The Item Group tree has carried the NFA branch (Suppressor / SBR / SBS / DD /
MG) since the taxonomy restructure, but the firearm_type Select never offered
NFA values, so an NFA item (e.g. a silencer received from RSR) could only be
booked as "Other Firearm". These tests pin the full chain:

  canonical vocabulary (firearm.ATF_FIREARM_TYPES) → every rendered copy
  (Select fixtures, JS dropdowns, patch seed) → _TYPE_TO_GROUP leaf
  resolution → firearm gating (is_firearm auto-stamp on the new AOW leaf).

firearm_type is a §478.125(e) bound-book column; FastBound's outbound
item-type allowlist (client._fb_item_type) passes every canonical value
through unchanged — the POS→FB→POS round trip is pinned on the
ffl_integrations side (test_fastbound_importer.FbItemTypeRoundTrip).
"""
from __future__ import annotations

import json
import os
import re

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core import firearm
from ffl_core.setup import item_groups

# ATF-worded firearm_type → Item Group leaf. AOW is the one leaf this
# feature adds; the other five predate it.
NFA_TYPE_TO_LEAF = {
	"Silencer": "Suppressor",
	"Short Barreled Rifle": "SBR",
	"Short Barreled Shotgun": "SBS",
	"Machine Gun": "MG",
	"Destructive Device": "DD",
	"Any Other Weapon": "AOW",
}


class NfaTypeToGroup(FrappeTestCase):
	def test_nfa_types_resolve_to_nfa_leaves_via_public_entry(self):
		# Through used_firearm_item_doc (the trade-in / used-intake entry),
		# not the private dict — this also exercises the
		# .get(..., USED_FIREARM) fallback resolution around it.
		for ftype, leaf in NFA_TYPE_TO_LEAF.items():
			doc = firearm.used_firearm_item_doc(
				item_code=f"NFA-VOCAB-{leaf}",
				item_name="t", description="t", firearm_type=ftype,
			)
			self.assertEqual(doc["item_group"], leaf, ftype)

	def test_aow_is_an_nfa_leaf_and_gates_as_firearm(self):
		# ALL_FIREARM_GROUPS membership is load-bearing (auto_set_is_firearm),
		# so the new leaf must ride along with the existing NFA leaves.
		self.assertIn("AOW", firearm.NFA_LEAVES)
		self.assertIn("AOW", firearm.ALL_FIREARM_GROUPS)

	def test_vocabulary_and_group_map_agree(self):
		# Cross-file invariants: every canonical type resolves to a group
		# that item_groups.ensure() actually creates — the two files carry
		# the group names as separate literals and would otherwise only fail
		# at runtime on a typo.
		tree_names = {name for name, _parent, _is_group in item_groups.TREE}
		for ftype in firearm.ATF_FIREARM_TYPES:
			self.assertIn(ftype, firearm._TYPE_TO_GROUP, ftype)
			self.assertIn(firearm._TYPE_TO_GROUP[ftype], tree_names, ftype)


class NfaGroupTree(FrappeTestCase):
	def test_ensure_creates_aow_leaf_under_nfa(self):
		item_groups.ensure()
		row = frappe.db.get_value(
			"Item Group", "AOW", ["parent_item_group", "is_group"], as_dict=True
		)
		self.assertIsNotNone(row, "AOW Item Group missing after ensure()")
		self.assertEqual(row.parent_item_group, "NFA")
		self.assertEqual(int(row.is_group or 0), 0)

	def test_aow_item_auto_stamps_is_firearm(self):
		# The Item doc carries no is_firearm and relies on the
		# auto_set_is_firearm before_save hook via group membership — the
		# same gate RSR promote's create path leans on. Unique code each run
		# so persistent-site residue can never skip the insert and turn the
		# assertion vacuous.
		item_groups.ensure()
		code = f"NFA-VOCAB-AOW-{frappe.generate_hash(length=10)}"
		doc = frappe.get_doc({
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": "AOW",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"has_serial_no": 1,
		}).insert(ignore_permissions=True)
		self.assertEqual(int(doc.is_firearm or 0), 1)


class NfaSelectOptions(FrappeTestCase):
	def test_live_meta_offers_the_full_vocabulary(self):
		# What the Desk form / Receive Goods page actually renders. Exact
		# equality so removals and reorders fail, not just missing values.
		for dt in ("Item", "Serial No"):
			field = frappe.get_meta(dt).get_field("firearm_type")
			self.assertEqual(
				field.options, firearm.FIREARM_TYPE_SELECT_OPTIONS,
				f"{dt}.firearm_type drifted from the canonical vocabulary",
			)

	def test_fixture_matches_canonical_vocabulary(self):
		# The fixture JSON is a hand-written copy (Frappe fixture format) —
		# pin it to the canonical string. The backfill patch needs no twin
		# guard: it imports FIREARM_TYPE_SELECT_OPTIONS directly.
		path = frappe.get_app_path("ffl_core", "fixtures", "custom_field.json")
		with open(path) as fh:
			rows = {r["name"]: r for r in json.load(fh)}
		for name in ("Item-firearm_type", "Serial No-firearm_type"):
			self.assertEqual(
				rows[name]["options"], firearm.FIREARM_TYPE_SELECT_OPTIONS, name
			)

	def test_js_surfaces_share_the_bundle_vocabulary(self):
		# The bundle defines ffl.FIREARM_TYPE_OPTIONS once; parse the literal
		# out and pin it to the canonical tuple — order included — so a JS-side
		# extra value, reorder, or respelling fails here, not at the counter.
		app = frappe.get_app_path("ffl_core")
		with open(os.path.join(app, "public", "js", "ffl_core.bundle.js")) as fh:
			bundle = fh.read()
		m = re.search(
			r"ffl\.FIREARM_TYPE_OPTIONS\s*=\s*\[(.*?)\]\.join\('\\n'\)",
			bundle, re.S,
		)
		self.assertIsNotNone(m, "ffl.FIREARM_TYPE_OPTIONS definition not found")
		values = re.findall(r"'([^']*)'", m.group(1))
		self.assertEqual(values, ["", *firearm.ATF_FIREARM_TYPES])
		for ftype in firearm.ATF_FIREARM_TYPES:
			self.assertEqual(
				bundle.count(f"'{ftype}'"), 1,
				f"'{ftype}' must appear exactly once in the bundle "
				"(the ffl.FIREARM_TYPE_OPTIONS definition)",
			)
		with open(os.path.join(
			app, "ffl_core", "page", "receive_goods", "receive_goods.js"
		)) as fh:
			page = fh.read()
		self.assertEqual(page.count("ffl.FIREARM_TYPE_OPTIONS"), 2)
		self.assertNotIn("Silencer", page, "receive_goods.js grew its own vocabulary copy")
