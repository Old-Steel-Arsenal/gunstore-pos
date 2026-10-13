"""Storage Locations page — stub controller.

Everything the page does is client-side calls to ffl_core.api.storage; the file must
exist so Frappe registers the page. The page is named `storage-map` (title "Storage
Locations") so its route never collides with the Storage Location DocType's own
route (see tests/test_desk_page_routes.py).
"""
from __future__ import annotations


def get_context(context):
	return context
