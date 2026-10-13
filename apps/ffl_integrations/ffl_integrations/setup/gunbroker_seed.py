"""GunBroker Settings seed — idempotent, and it never overwrites an operator.

Runs on after_install and after_migrate. Both tables it fills are things a store
will edit (categories are a merchandising decision, fee tiers change when
GunBroker changes its rate card), so the rule is: seed a table only when it is
EMPTY. A migrate must never quietly reinstate a row somebody deleted on purpose,
and it must never reset a rate somebody corrected.
"""
from __future__ import annotations

import frappe

from ffl_core.utils import single_raw_map

SETTINGS_DOCTYPE = "GunBroker Settings"

# (firearm_type, is_cr_eligible, category_id, category_name)
#
# Only the four types with an unambiguous home get a row. The category IDs are
# GunBroker leaf categories (canContainItems=true) read off the sandbox account;
# whether the production tree uses the same numbers has NOT been proven, so every
# one of them is re-checked against production before go-live.
#
# Rifles and shotguns land in the generic "Other" leaves on purpose. Picking
# between Bolt Action / Lever Action / Semi Auto needs an action type this POS
# does not record, and guessing wrong buries the listing in a category buyers
# filter past. A specific model that deserves a specific category gets it from
# the Item's own GunBroker Category ID override.
#
# NOT seeded, deliberately:
#   * Curios & Relics (2323). The plumbing is here — tick "C&R Eligible" on a row
#     and any gun made 50+ years ago matches it first — but the store opts in by
#     adding that row. The frozen decision (LD-4) makes 3025 the DEFAULT for a
#     rifle and C&R an option, and routing every surplus rifle out of the rifle
#     categories is a merchandising call, not a migration's to make.
#   * Receiver / Other Firearm / every NFA type. No row means the listing is
#     refused with "no category", which is the right answer: NFA items are
#     out of scope for this channel entirely, and a stripped receiver in the
#     wrong category is a policy problem, not a lost sale.
DEFAULT_CATEGORY_MAP: tuple[tuple[str, int, int, str], ...] = (
	("Pistol", 0, 3026, "Semi Auto Pistols"),
	("Revolver", 0, 2325, "Revolvers"),
	("Rifle", 0, 3025, "Other Rifles"),
	("Shotgun", 0, 3108, "Other Shotguns"),
)

# (upper_bound, rate) — GunBroker's final value fee, charged per item on a
# graduated basis. upper_bound 0 means "everything above the previous band" and
# must stay last.
DEFAULT_FEE_TIERS: tuple[tuple[float, float], ...] = (
	(400.0, 6.0),
	(15000.0, 4.0),
	(20000.0, 3.0),
	(0.0, 2.0),
)


def _restore_missing_defaults(settings) -> bool:
	"""Put back any docfield default that reads as None, before saving.

	Belt to the `seed_gunbroker_single_defaults` patch's braces, and it earns its
	place because this function is the thing that would otherwise CAUSE the
	damage: a Single applies its docfield defaults only while it has no stored
	rows at all, so on a site where any row exists every other field loads
	blank — and the `save()` below writes the whole document back, turning those
	blanks into stored rows. Restoring them here means that even if this runs
	before the patch, or the patch is skipped, or a field is added later after
	the patch has already run once, the save cannot quietly blank the settings
	that decide whether a listing says it needs an FFL.

	Returns True when anything was restored, so the caller saves.
	"""
	restored = False
	# One query for the whole Single, rather than one per field. Migrate-time
	# performance is irrelevant here; using the batched reader that lives beside
	# `single_raw` just keeps one obvious way of asking this question.
	stored = single_raw_map(SETTINGS_DOCTYPE)
	for field in frappe.get_meta(SETTINGS_DOCTYPE).fields:
		default = field.get("default")
		if default is None or str(default).strip() == "":
			continue
		# The STORED rows, not `settings.get(...)`. A Single load casts every Check
		# through `cint()` unconditionally, so an unsaved checkbox arrives as 0 —
		# reading the document could never tell "never saved" from "unchecked",
		# and this loop would skip every Check field there is.
		if field.fieldname in stored:
			continue
		settings.set(field.fieldname, default)
		restored = True
	return restored


def run() -> None:
	"""Fill the two Settings child tables if they are empty. Safe to re-run."""
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	dirty = _restore_missing_defaults(settings)

	if not settings.get("category_map"):
		for firearm_type, is_cr, category_id, category_name in DEFAULT_CATEGORY_MAP:
			settings.append("category_map", {
				"firearm_type": firearm_type,
				"is_cr_eligible": is_cr,
				"category_id": category_id,
				"category_name": category_name,
			})
		dirty = True

	if not settings.get("fee_tiers"):
		for upper_bound, rate in DEFAULT_FEE_TIERS:
			settings.append("fee_tiers", {"upper_bound": upper_bound, "rate": rate})
		dirty = True

	if not dirty:
		return

	# ignore_permissions: after_migrate runs as Administrator, but bench execute
	# can be run by whoever is at the terminal, and a seed that fails on a
	# permission check leaves the store with an empty category map and no listing
	# path at all.
	settings.flags.ignore_permissions = True
	# ignore_mandatory / ignore_validate: this Single is seeded BEFORE anybody has
	# filled in credentials, and its validate() refuses to save an enabled
	# integration with no deposit account. Seeding must not be able to fail on
	# rules about a configuration nobody has entered yet — those rules run when a
	# human saves the form, which is when they are the right question to ask.
	settings.flags.ignore_mandatory = True
	# ignore_validate: the form's own validate() refuses to save an ENABLED
	# integration that has no deposit account. That rule belongs to a human saving
	# the form; a migrate filling in a lookup table must not be able to fail on it
	# and take the whole migrate down with it.
	settings.flags.ignore_validate = True
	settings.save(ignore_permissions=True)
