"""One-shot — write the Pending Order overdue threshold onto existing FFL
Settings. A Single applies docfield defaults only while it has NO stored rows
(see ffl_integrations.patches.seed_distributor_freight_defaults); every site
already has FFL Settings rows, so the new field would read back blank and the
digest would fall back to its code default silently. Only written when no row
exists, so a stated value stays.
"""
from __future__ import annotations

import frappe

from ffl_core.utils import single_raw


def execute() -> None:
	field = frappe.get_meta("FFL Settings").get_field("pending_order_age_alert_days")
	if not field or field.get("default") in (None, ""):
		return
	if single_raw("FFL Settings", "pending_order_age_alert_days") is not None:
		return
	frappe.db.set_single_value("FFL Settings", "pending_order_age_alert_days", field.default)
