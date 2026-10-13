"""Receive Goods page — stub controller.

Frappe pages don't need a Python controller for what we're doing
(everything's client-side calling whitelisted methods in
ffl_core.api.receive_goods), but the file must exist so Frappe registers
the page.
"""
from __future__ import annotations

import frappe


def get_context(context):
	return context
