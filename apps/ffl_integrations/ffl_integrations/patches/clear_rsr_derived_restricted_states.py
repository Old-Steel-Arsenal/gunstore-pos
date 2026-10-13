"""Clear `Item.restricted_states` values that came from the RSR inventory feed.

**DELIBERATELY NOT REGISTERED IN patches.txt.** This is a defensive fallback, not a
migration step. Run it by hand only, and only when the situation below actually
applies::

    bench --site <site> execute \\
        ffl_integrations.patches.clear_rsr_derived_restricted_states.run

Why it exists
-------------
Until the ship-restriction feed replaced it, `Item.restricted_states` was populated from the RSR inventory
file's per-state Y-flags — read at columns 16-66 while RSR's manual says "fields
16-56". Nobody reconciled the two, and a wrong offset does not crash: it mislabels
which states an item is restricted in, silently.

That field is not decorative. It reaches WooCommerce as `_osa_restricted_states`
(the storefront guard), and ffl_core's `_validate_items` **throws** on it for a
counter drop-ship line. So the guessed columns could refuse counter orders.

That change cut the feed as the source and handed the field to the operator. The problem
that creates: values already written by the old pipeline do not disappear, nothing
updates them any more, and they keep driving both consumers — guessed data frozen
in place permanently, which is worse than the bug being fixed.

Why it is NOT registered
------------------------
Production was measured before this was written: `tabItem` has neither the
`dropship_distributor` nor the `restricted_states` column, and `tabCustom Field`
has no records for them — the P3 listing fixtures were never deployed. Exposure is
**zero**, so there is nothing to clean and an automatic patch would be a no-op that
future readers would have to re-derive the purpose of.

It is kept because the exposure becomes non-zero the moment the listing pipeline
ships and runs even once against RSR data. This is the way back, already written
and already reasoned about, rather than something to invent under pressure.

Reversibility
-------------
What it clears is exactly the RSR inventory file's state bits. Re-running the
earlier listing pipeline would reproduce them, so this destroys nothing that
cannot be regenerated from the feed. It deliberately does NOT touch Items whose
`dropship_distributor` is empty or points at another distributor — a value there
was either hand-entered or came from somewhere else, and neither is ours to erase.
"""
from __future__ import annotations

import frappe

RSR_PROVIDER = "rsr"


def _rsr_distributors() -> list:
	"""Distributor codes served by the RSR provider — the feed we are undoing.

	Read from the Distributor table rather than hardcoded "RSR": the code is
	operator-chosen (the integration tests use "P3-DC"), so a literal would quietly
	clean nothing on a site that named it anything else.
	"""
	return frappe.get_all(
		"Distributor", filters={"provider": RSR_PROVIDER}, pluck="name") or []


def run(*, dry_run: bool = True) -> dict:
	"""Report (default) or clear. Returns ``{items, distributors, cleared}``.

	``dry_run=True`` by DEFAULT and on purpose: this is a destructive write to a
	compliance-adjacent field, and the first thing anyone running it should get is
	a count, not a result. Clear for real with::

	    bench --site <site> execute \\
	        ffl_integrations.patches.clear_rsr_derived_restricted_states.run \\
	        --kwargs "{'dry_run': False}"
	"""
	if not frappe.db.has_column("Item", "restricted_states"):
		# The field does not exist on this site (the state production is in as of
		# 2026-07-28). Nothing to do, and saying so beats a silent zero.
		return {"items": 0, "distributors": [], "cleared": 0,
			"note": "Item.restricted_states does not exist on this site"}

	distributors = _rsr_distributors()
	if not distributors:
		return {"items": 0, "distributors": [], "cleared": 0,
			"note": "no Distributor rows use the RSR provider"}

	names = frappe.get_all(
		"Item",
		filters={"dropship_distributor": ("in", distributors),
			"restricted_states": ("!=", "")},
		pluck="name") or []
	if dry_run or not names:
		return {"items": len(names), "distributors": distributors, "cleared": 0,
			"dry_run": True}

	for name in names:
		# update_modified=False: this is a data correction, not an edit anyone made,
		# and bumping `modified` on every drop-ship Item would make the change look
		# like operator activity in every audit view that sorts by it.
		frappe.db.set_value("Item", name, "restricted_states", "",
			update_modified=False)
	frappe.db.commit()
	return {"items": len(names), "distributors": distributors,
		"cleared": len(names), "dry_run": False}
