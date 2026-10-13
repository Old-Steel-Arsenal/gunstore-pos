"""Distributor Restock page — stub controller.

Everything is client-side, calling the whitelisted, provider-agnostic
``ffl_integrations.distributor.api`` methods (search / check_availability /
create_orders / confirm_order). The file exists so Frappe registers the page.
"""
from __future__ import annotations

import frappe


def get_context(context):
	return context
