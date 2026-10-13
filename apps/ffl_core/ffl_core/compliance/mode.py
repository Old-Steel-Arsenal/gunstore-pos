"""Store-level compliance mode switch: is the 4473 handled in this system or on paper?

One reader, so every consumer — the submit gate, the POS payment screen, the
bound-book booking path — agrees on which mode the store is in.

``FFL Settings.paper_4473_mode`` OFF (the default, and what every existing site
migrates to) is the electronic flow this app was built around: the counter clicks
FastBound 4473, FastBound collects the form and runs NICS, and its webhook stamps
``form_4473_status`` / ``nics_status`` plus a Retail Sale FFL Disposition per gun.

ON means the store keeps paper 4473s and runs NICS by phone or the FBI portal —
the normal setup once FastBound Settings.enabled is unchecked. POS then stops
waiting for a webhook that will never arrive and asks the cashier to attest that
the paper form is done (``fastbound.pos_4473.attest_paper_4473``), which books the
same bound-book records marked ``manual_override``.

The switch changes only where the 4473 record lives. It does not relax anything
the ATF requires (27 CFR 478.102 / 478.124): the paper form must still be
completed and filed, and the background check still has to be run and cleared
before the gun is handed over. Nothing in this system can verify that, which is
exactly why the attestation records WHO said it was done.
"""
from __future__ import annotations

from ffl_core.utils import single_flag

SETTINGS_DOCTYPE = "FFL Settings"


def paper_4473_mode() -> bool:
	"""True when 4473 / NICS are handled outside this system (paper mode)."""
	return single_flag(SETTINGS_DOCTYPE, "paper_4473_mode", default=0)
