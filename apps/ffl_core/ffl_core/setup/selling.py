"""Pin ERPNext's "Allow Negative rates for Items" on.

Trade-in credit (setup/trade_in_credit) is booked as a NEGATIVE-rate goods
line — required for the Texas trade-in tax treatment (§151.007(c)(3): the
negative line pulls the taxable base down). A POS Invoice accepts a negative
line, but the consolidated Sales Invoice runs ERPNext's `validate_qty`
(controllers/status_updater.py), which REJECTS a negative item rate unless
Selling Settings `allow_negative_rates_for_items` is on. With it off, every
trade-in submits fine at the register but then fails to
consolidate — and for a firearm (consolidated immediately on submit, see
api/pos_consolidate) the gun silently never leaves ERPNext stock.

So we pin the flag on in code (after_install + after_migrate), making it
self-healing across deploys, migrations, fresh installs and DB restores instead
of a manual UI setting that can drift off. The negative credit item is already
tamper-gated (compliance/trade_in_credit: rate must equal the recorded cost,
single-use, a below-zero ticket only as a trade-in payout), so the global
"accidental negative rate" guard this disables adds little for our flows — and
it never applied to POS Invoice in the first place.

Selling side only: ffl_core has no negative-rate BUY lines (a trade-in payout
that exceeds the sale is a separate positive-cost Receive Goods/purchase).
"""
from __future__ import annotations

import frappe

_SETTING = "allow_negative_rates_for_items"


def ensure() -> None:
	"""Turn on Selling Settings `allow_negative_rates_for_items`. Idempotent;
	never raises (runs in after_migrate, where a thrown hook aborts the whole
	`bench migrate`) — failures are logged and the next run retries."""
	try:
		if not frappe.db.get_single_value("Selling Settings", _SETTING):
			frappe.db.set_single_value("Selling Settings", _SETTING, 1)
	except Exception:
		frappe.log_error(
			title="ffl_core.setup.selling: ensure failed",
			message=frappe.get_traceback())
