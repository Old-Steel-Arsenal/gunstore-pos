"""One-shot: write WooCommerce Settings.follow_category_tree = 1 where no row exists.

The retail store follows the catalog category tree by default,
but a Check with docfield default 1 has no ``tabSingles`` row on a site whose
settings were saved before the field existed: reads go through ``single_flag``
(which honours the default), yet the form loads the missing Check as 0 — and the
next save of the settings page would store that 0 and silently take the retail
store out of the mirror. Writing the row makes the form show the truth. Same
defence as ``ffl_integrations.patches.seed_require_card_reference``.

Retail only: the dealer store's default is 0, which a missing row already reads
as. Only ever writes where nothing has been stated, so re-running can never
overturn a deliberate choice.
"""
from __future__ import annotations

import frappe

from ffl_core.utils import single_raw


def execute() -> None:
	if single_raw("WooCommerce Settings", "follow_category_tree") is None:
		frappe.db.set_single_value("WooCommerce Settings", "follow_category_tree", 1)
