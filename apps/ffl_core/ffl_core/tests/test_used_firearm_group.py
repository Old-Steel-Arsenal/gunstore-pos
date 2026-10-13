# apps/ffl_core/ffl_core/tests/test_used_firearm_group.py
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ffl_core import firearm
from ffl_core.firearm import used_firearm_item_doc


class UsedFirearmItemGroup(FrappeTestCase):
    """Layer C (T2): firearm_type → Item Group leaf.

    Pistol and Revolver map to their OWN leaves (both live Woo categories carry
    stock: 35 / 5); the legacy dead alias "Handgun" folds into Pistol.  This
    closes the Category-filter recurrence source: ``sync._apply_category``
    derives the Woo category from ``item_group``, so new intake must land on
    Pistol / Revolver and never re-create the empty "Handgun" category.
    """

    def _group(self, ftype):
        return used_firearm_item_doc(
            item_code="T2-GUN", item_name="T2 Gun", description="",
            firearm_type=ftype,
        )["item_group"]

    def test_pistol_maps_to_pistol(self):
        self.assertEqual(self._group("Pistol"), "Pistol")

    def test_revolver_maps_to_revolver(self):
        self.assertEqual(self._group("Revolver"), "Revolver")

    def test_legacy_handgun_folds_into_pistol(self):
        self.assertEqual(self._group("Handgun"), "Pistol")

    def test_legacy_long_gun_folds_into_rifle(self):
        # "Long Gun" is never created on a fresh site (item_groups.LEGACY_LEAVES).
        self.assertEqual(self._group("Long Gun"), "Rifle")

    def test_other_firearm_types_unchanged(self):
        self.assertEqual(self._group("Rifle"), "Rifle")
        self.assertEqual(self._group("Shotgun"), "Shotgun")
        self.assertEqual(self._group("Receiver"), "Receiver")  # owner 2026-09-26
        self.assertEqual(self._group("Other Firearm"), "Other Firearm")

    def test_blank_type_falls_back_to_other_firearm(self):
        # The legacy "Used Firearm" group is retired.
        self.assertEqual(self._group(""), "Other Firearm")
        self.assertEqual(self._group(None), "Other Firearm")

    def test_handgun_constant_retained_for_firearm_gating(self):
        # HANDGUN is only UNBOUND from _TYPE_TO_GROUP; the constant stays and
        # must remain a recognised firearm leaf (legacy items still pinned to
        # "Handgun" continue to gate as firearms via ALL_FIREARM_GROUPS).
        self.assertEqual(firearm.HANDGUN, "Handgun")
        self.assertIn(firearm.HANDGUN, firearm.ALL_FIREARM_GROUPS)


class FirearmItemGroupTaxonomy(FrappeTestCase):
    """Layer C companion: the canonical taxonomy must define Pistol / Revolver
    as Firearm leaves so ``_TYPE_TO_GROUP``'s targets exist — used_firearm_item_doc
    and RSR promote insert Items into them.  Matches prod (both leaves exist since
    2026-07-21); ``ensure()`` heals dev / CI idempotently at after_migrate.
    """

    def test_ensure_creates_pistol_and_revolver_firearm_leaves(self):
        from ffl_core.setup.item_groups import ensure
        ensure()
        for leaf in ("Pistol", "Revolver"):
            with self.subTest(leaf=leaf):
                self.assertTrue(
                    frappe.db.exists("Item Group", leaf),
                    f"{leaf} Item Group must exist after ensure()",
                )
                row = frappe.db.get_value(
                    "Item Group", leaf, ["parent_item_group", "is_group"], as_dict=True
                )
                self.assertEqual(row.parent_item_group, "Firearm")
                self.assertEqual(int(row.is_group or 0), 0)

    def test_ensure_never_recreates_legacy_leaves(self):
        # Kept where a site still has them, never created: a fresh install has no
        # Items keyed to them, and one retired by patches/retire_legacy_item_groups
        # must stay gone after the next migrate's after_migrate ensure().
        from ffl_core.setup import item_groups
        frappe.db.savepoint("legacy_leaves")
        try:
            for name in item_groups.LEGACY_LEAVES:
                frappe.db.delete("Item Group", {"name": name})
            item_groups.ensure()
            for name in item_groups.LEGACY_LEAVES:
                with self.subTest(leaf=name):
                    self.assertFalse(frappe.db.exists("Item Group", name))
            self.assertTrue(frappe.db.exists("Item Group", "Pistol"))
        finally:
            frappe.db.rollback(save_point="legacy_leaves")


class RetireLegacyItemGroups(FrappeTestCase):
    """patches/retire_legacy_item_groups: an unused leaf is deleted; one that
    anything still links to is kept with its nested-set position untouched
    (delete_doc edits the tree in on_trash before its link check)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from ffl_core.setup import test_bootstrap
        test_bootstrap.run()

    def _leaf(self, name):
        if not frappe.db.exists("Item Group", name):
            frappe.get_doc({
                "doctype": "Item Group", "item_group_name": name,
                "parent_item_group": "All Item Groups", "is_group": 0,
            }).insert(ignore_permissions=True)
        return name

    def test_unused_leaf_is_deleted(self):
        from ffl_core.patches.retire_legacy_item_groups import retire
        name = self._leaf("_T Retire Unused")
        self.assertEqual(retire([name, "_T Retire Never Existed"]), [name])
        self.assertFalse(frappe.db.exists("Item Group", name))

    def test_linked_leaf_is_kept_with_tree_intact(self):
        from ffl_core.patches.retire_legacy_item_groups import retire
        name = self._leaf("_T Retire Linked")
        if not frappe.db.exists("Item", "_T-RETIRE-LINKED"):
            frappe.get_doc({
                "doctype": "Item", "item_code": "_T-RETIRE-LINKED", "item_name": "_T-RETIRE-LINKED",
                "item_group": name, "stock_uom": "Nos", "is_stock_item": 0,
            }).insert(ignore_permissions=True)
        fields = ["lft", "rgt", "parent_item_group"]
        before = frappe.db.get_value("Item Group", name, fields)
        self.assertEqual(retire([name]), [])
        self.assertEqual(frappe.db.get_value("Item Group", name, fields), before)


class RetireNewUsedFirearmGroups(FrappeTestCase):
    """patches/retire_new_used_firearm_item_groups: New Firearm /
    Used Firearm / Long Gun are deleted where nothing links to them and kept — still
    gating as firearms — where something does; ensure() never brings them back."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from ffl_core.setup import test_bootstrap
        test_bootstrap.run()

    def _root_leaf(self, name):
        if not frappe.db.exists("Item Group", name):
            frappe.get_doc({"doctype": "Item Group", "item_group_name": name,
                "parent_item_group": "All Item Groups", "is_group": 0}).insert(ignore_permissions=True)

    def test_unlinked_groups_are_deleted_a_linked_one_kept(self):
        from ffl_core.patches import retire_new_used_firearm_item_groups as patch
        from ffl_core.setup import item_groups
        frappe.db.savepoint("retire_new_used")
        try:
            for name in ("New Firearm", "Used Firearm", "Long Gun"):
                # Items an earlier run left keyed to a legacy group move off it.
                frappe.db.sql("UPDATE `tabItem` SET item_group='Pistol' WHERE item_group=%s", name)
                self._root_leaf(name)
            frappe.get_doc({"doctype": "Item", "item_code": "_T-RETIRE-USED-FA",
                "item_name": "_T-RETIRE-USED-FA", "item_group": "Used Firearm",
                "stock_uom": "Nos", "is_stock_item": 0}).insert(ignore_permissions=True)
            patch.execute()
            self.assertFalse(frappe.db.exists("Item Group", "New Firearm"))
            self.assertFalse(frappe.db.exists("Item Group", "Long Gun"))
            self.assertTrue(frappe.db.exists("Item Group", "Used Firearm"))   # an Item links to it
            self.assertTrue(firearm.is_firearm_item("_T-RETIRE-USED-FA"))    # and it still gates
            item_groups.ensure()
            self.assertFalse(frappe.db.exists("Item Group", "New Firearm"))
        finally:
            frappe.db.rollback(save_point="retire_new_used")

    def test_constants_stay_firearm_groups(self):
        for name in (firearm.NEW_FIREARM, firearm.USED_FIREARM, firearm.LONG_GUN):
            self.assertIn(name, firearm.ALL_FIREARM_GROUPS)


class PistolRevolverFirearmGating(FrappeTestCase):
    """Compliance gating: Pistol / Revolver are firearm leaves, so an Item in
    either group must gate as a firearm even WITHOUT an explicit is_firearm flag.

    This matters because RSR ``promote_to_item`` builds its Item doc with NO
    is_firearm field (promote.py) — it relies on the ``auto_set_is_firearm``
    before_save hook, which stamps is_firearm only when
    ``item_group in ALL_FIREARM_GROUPS``.  With dept 01/02 now targeting
    "Pistol", the leaf MUST be in ALL_FIREARM_GROUPS or promoted handguns escape
    firearm gating (bound-book / 4473 / FastBound).
    """

    def test_pistol_and_revolver_are_firearm_groups(self):
        for leaf in ("Pistol", "Revolver"):
            with self.subTest(leaf=leaf):
                self.assertIn(leaf, firearm.ALL_FIREARM_GROUPS)

    def test_group_alone_gates_as_firearm_without_flag(self):
        # _is_firearm_row: is_firearm flag OR firearm-group membership.
        for leaf in ("Pistol", "Revolver"):
            with self.subTest(leaf=leaf):
                self.assertTrue(
                    firearm._is_firearm_row({"is_firearm": 0, "item_group": leaf}),
                    f"{leaf}-group item must gate as a firearm without an explicit flag",
                )

    def test_pistol_group_item_is_firearm_item_true(self):
        # A Pistol-group Item created WITHOUT an explicit is_firearm flag (the RSR
        # promote shape) must still resolve as a firearm — the auto hook stamps it
        # and the group is a firearm group regardless.
        from ffl_core.setup.item_groups import ensure
        ensure()
        code = "PRG-PISTOL-GATE"
        if not frappe.db.exists("Item", code):
            frappe.get_doc({
                "doctype": "Item", "item_code": code, "item_name": code,
                "item_group": "Pistol", "stock_uom": "Nos", "is_stock_item": 1,
            }).insert(ignore_permissions=True)
        self.assertTrue(firearm.is_firearm_item(code))
