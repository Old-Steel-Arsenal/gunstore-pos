"""Store-level switches for optional core behaviour, read from FFL Settings.

The same code serves more than one shop, and a store that does not want a
behaviour says so here; the readers sit on ``ffl_core.utils.single_flag``, which
honours the docfield default on a site that has never saved the field.

Not here: outbound consignment. It is an extension (``osa_consignment``) and
its per-store switch is its ``POS Extension`` row, read through
``ffl_core.extensions.require_enabled`` / ``is_enabled`` — the same idea as
this module, different owner.
"""
from __future__ import annotations

from ffl_core.utils import single_flag

SETTINGS_DOCTYPE = "FFL Settings"


def customer_id_numbers_enabled() -> bool:
	"""True when FFL Settings → Store Customer ID Numbers is ticked.

	Defaults OFF: off is how every store ran before the switch existed (no ID
	number or date of birth on the Customer), so a site that has never saved the
	field keeps that behaviour and needs no defaults patch. ``ffl_core.customer_profile.validate_customer`` reads it on
	every Customer save; ``ffl_integrations.boot`` sends it to the browser.
	"""
	return single_flag(SETTINGS_DOCTYPE, "store_customer_id_numbers", default=0)
