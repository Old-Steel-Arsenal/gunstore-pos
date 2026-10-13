"""Retire the legacy root Item Groups "New Firearm", "Used Firearm" and "Long Gun"
 New vs used is the Item's ``is_used`` checkbox and every
firearm files under its type leaf (Pistol / Revolver / Rifle / Shotgun / Other
Firearm); these roots sit outside the catalog master's tree and are no longer
needed. ``item_groups.ensure()`` no longer creates them, so a deletion sticks.

Same rule as ``retire_legacy_item_groups``: a group anything still links to (an
Item, a submitted line, a default) is kept — history is never orphaned — and
the constants stay in ``ALL_FIREARM_GROUPS`` so such a site keeps gating its
Items as firearms. Idempotent.
"""
from __future__ import annotations

from ffl_core.firearm import LONG_GUN, NEW_FIREARM, USED_FIREARM
from ffl_core.patches.retire_legacy_item_groups import retire


def execute():
	retired = retire((NEW_FIREARM, USED_FIREARM, LONG_GUN))
	if retired:
		print(f"retire_new_used_firearm_item_groups: deleted {', '.join(retired)}")
