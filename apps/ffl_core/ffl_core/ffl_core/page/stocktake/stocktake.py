"""Inventory Count page — stub controller.

Everything the page does is client-side calls to ffl_core.api.inventory_count;
the file must exist so Frappe registers the page. The page is named `stocktake`
because a Page named `inventory-count` would collide with the Inventory Count
DocType's own route (see tests/test_desk_page_routes.py); its title is still
"Inventory Count".
"""
from __future__ import annotations


def get_context(context):
	return context
